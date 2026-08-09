#!/usr/bin/env python3
"""test_emotion_noise.py — ② 情绪自然波动（OU 噪声）单元测试（TDD）"""

import math
import os
import random
import re
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_math import ou_step, noise_cap
from chiguo_state import ChiguoState


# ── 纯函数 ou_step ──────────────────────────────────────────────

def test_ou_step_zero_dt_identity():
    """dt<=0 → 恒等（不产生噪声）；sigma<=0 → 恒等。"""
    rng = random.Random(42)
    assert ou_step(50.0, 100.0, 0.5, 0.3, 0.0, rng) == 50.0
    assert ou_step(50.0, 100.0, 0.5, 0.3, -1.0, rng) == 50.0
    assert ou_step(50.0, 100.0, 0.5, 0.0, 1.0, rng) == 50.0


def test_ou_step_deterministic_seed():
    """固定 seed → 序列完全确定（可复现）。"""
    r1, r2 = random.Random(7), random.Random(7)
    out1 = [ou_step(0.0, 0.0, 0.5, 0.3, 0.1, r1) for _ in range(50)]
    out2 = [ou_step(0.0, 0.0, 0.5, 0.3, 0.1, r2) for _ in range(50)]
    assert out1 == out2


def test_ou_step_statistics():
    """统计性质：迭代 OU 过程（burn-in 后）样本均值≈0、标准差≈σ/√(2θ)。

    OU 平稳分布 N(μ, σ²/2θ)：σ=0.3、θ=0.5 → 理论 std ≈ 0.3。
    不断言单步具体值（脆断），只断言统计性质。
    """
    rng = random.Random(42)
    n, burn = 5000, 1000
    vals = [0.0]
    for _ in range(n):
        vals.append(ou_step(vals[-1], 0.0, 0.5, 0.3, 0.1, rng))
    sample = vals[burn:]
    mean = sum(sample) / len(sample)
    var = sum((x - mean) ** 2 for x in sample) / len(sample)
    std = math.sqrt(var)
    # 均值在 ±3σ̂/√N 内
    assert abs(mean) < 3 * std / math.sqrt(len(sample)), f"均值应≈0: {mean}"
    # 标准差在理论值 ±10%
    assert abs(std - 0.3) < 0.03, f"std 应≈0.3: {std}"


def test_ou_step_mean_reversion():
    """均值回归：target=0、θ 大 → 长期均值≈0（带回归的漂移而非随机游走）。"""
    rng = random.Random(1)
    # 从远离 target 的 5.0 出发，θ=0.5、dt=0.1 → pull = 0.5*(0-5)*0.1 = -0.25/步
    vals = [5.0]
    for _ in range(2000):
        vals.append(ou_step(vals[-1], 0.0, 0.5, 0.3, 0.1, rng))
    assert abs(vals[-1]) < 1.0, f"应被拉回 target 附近: {vals[-1]}"


# ── 纯函数 noise_cap ────────────────────────────────────────────

def test_noise_cap_bounds():
    """噪声绝对值 ≤ 0.5 × 弹性步进量（防 噪声>信号 反噬）。"""
    assert abs(noise_cap(1.0, 5.0)) <= 0.5
    assert abs(noise_cap(1.0, -5.0)) <= 0.5
    assert noise_cap(1.0, 0.0) == 0.0
    # 步进为 0（gap≈0）→ 噪声被压到 0（最坏情形防反噬）
    assert noise_cap(0.0, 3.0) == 0.0
    # 噪声本身小于上限 → 原样
    assert noise_cap(10.0, 0.2) == 0.2


# ── 行为级：tick 恒等 + 开启统计 ────────────────────────────────

def _make_state(temp_dir: str, **emo_overrides) -> ChiguoState:
    """构造临时目录中的 ChiguoState（隔离配置/状态文件）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(temp_dir)
    return ChiguoState(cfg)


def test_tick_disabled_identity():
    """noise_enabled=0（默认）→ tick 结果与无噪声版本逐位相等。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.emotion.loneliness = 60.0
        st.emotion.anxiety = 50.0
        # 记录关闭时的 tick 结果
        st.tick(1.0, now)
        lo_off, anx_off = st.emotion.loneliness, st.emotion.anxiety

        st2 = _make_state(td)
        st2.emotion.loneliness = 60.0
        st2.emotion.anxiety = 50.0
        st2.tick(1.0, now)
        lo2, anx2 = st2.emotion.loneliness, st2.emotion.anxiety
        assert lo_off == lo2 and anx_off == anx2, \
            "默认关闭 → 与现状逐位相等"


def test_tick_enabled_stays_in_bounds():
    """开启噪声（σ=0.3）→ 多次 tick 后值仍在 [0,100]（clamp 兜底）且不被噪声带飞。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["noise_enabled"] = 1
        st.config["emotion"]["noise_seed"] = 42
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.emotion.loneliness = 60.0
        st.emotion.anxiety = 50.0
        vals = []
        for i in range(200):
            st.tick(0.25, now + timedelta(minutes=15 * i))
            vals.append((st.emotion.loneliness, st.emotion.anxiety))
        for lo, anx in vals:
            assert 0.0 <= lo <= 100.0 and 0.0 <= anx <= 100.0
        # 噪声不改变收敛方向：loneliness 从 60 向 100、anxiety 从 50 向 100
        assert vals[-1][0] > 60.0 and vals[-1][1] > 50.0


def test_tick_enabled_does_not_pollute_global_rng():
    """独立 RNG：开启噪声后全局 random 序列不被消费（seed(42) 复现性保持）。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["noise_enabled"] = 1
        st.config["emotion"]["noise_seed"] = 42
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        # 开启噪声 tick 一次
        st.tick(1.0, now)
        # 全局 random 序列应与未开启时一致
        random.seed(42)
        expected = [random.random() for _ in range(10)]
        random.seed(42)
        st2 = _make_state(td)
        st2.config["emotion"]["noise_enabled"] = 1
        st2.config["emotion"]["noise_seed"] = 43  # 不同噪声种子
        st2.tick(1.0, now)
        actual = [random.random() for _ in range(10)]
        assert expected == actual, "噪声不得消费全局 random 序列"


if __name__ == "__main__":
    tests = [
        test_ou_step_zero_dt_identity, test_ou_step_deterministic_seed,
        test_ou_step_statistics, test_ou_step_mean_reversion,
        test_noise_cap_bounds,
        test_tick_disabled_identity, test_tick_enabled_stays_in_bounds,
        test_tick_enabled_does_not_pollute_global_rng,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
