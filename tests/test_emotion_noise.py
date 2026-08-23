#!/usr/bin/env python3
"""test_emotion_noise.py — ② 情绪自然波动（OU 噪声）单元测试（TDD）"""

import math
import os
import random
import re
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

def _make_state(temp_dir: str) -> ChiguoState:
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
    """noise_enabled=0（默认）→ 与 sigma=0 的 OU（纯恒等）结果逐位一致。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.emotion.loneliness = 60.0
        st.emotion.anxiety = 50.0
        st.tick(1.0, now)
        lo_off, anx_off = st.emotion.loneliness, st.emotion.anxiety

        st2 = _make_state(td)
        st2.config["emotion"]["noise_enabled"] = 1
        st2.config["emotion"]["noise_loneliness_sigma"] = 0.0  # σ=0 → ou_step 恒等
        st2.config["emotion"]["noise_anxiety_sigma"] = 0.0
        st2.emotion.loneliness = 60.0
        st2.emotion.anxiety = 50.0
        st2.tick(1.0, now)
        assert lo_off == st2.emotion.loneliness and anx_off == st2.emotion.anxiety, \
            "默认关闭 与 σ=0 开启 应逐位一致"


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


# ── A4: loop 常驻模式噪声增量语义（防重复累加漂移） ──────────────

def test_noise_loop_delta_increment_semantics():
    """A4 修复回归：loop 常驻模式连续 tick 时，加到 emotion 的是"本次增量"
    而非"完整 OU 状态"。OU 内存态 x 是累积的（x += θ(0−x)Δt + σ√Δt·ε），
    bug 版每 tick 把完整 x_i 加上 → 前 N-1 次噪声被重复累加 → 漂移放大。

    用 spy 捕获传给 noise_cap 的 raw 噪声值，断言 telescoping 恒等：
    Σ delta_i = x_final − x_0 = x_final（x_0=0），即每 tick 传的是
    x_i − x_{i-1} 而非完整 x_i。bug 版 Σ raw = Σ x_i ≠ x_N → 本测试失败。

    PR-4 起 noise_enabled/seed 归档至 [experimental]，需进归档段；PR-2 起
    噪声实现位于 state/emotion.EmotionMixin，spy 需同时覆盖 chiguo_math 与 state/emotion。"""
    import chiguo_math as cm
    import state.emotion as em_mod
    orig_cm = cm.noise_cap
    orig_em = em_mod.noise_cap
    calls = []

    def spy(step_magnitude, raw_noise):
        calls.append(raw_noise)
        return orig_cm(step_magnitude, raw_noise)

    cm.noise_cap = spy
    em_mod.noise_cap = spy
    try:
        with tempfile.TemporaryDirectory() as td:
            st = _make_state(td)
            st.config["emotion"]["noise_enabled"] = 1
            st.config["emotion"]["noise_seed"] = 42
            now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
            st.emotion.loneliness = 60.0
            st.emotion.anxiety = 50.0
            n_ticks = 200
            for i in range(n_ticks):
                st.tick(0.25, now + timedelta(minutes=15 * i))
            # 每 tick 恰好 2 次 noise_cap（loneliness + anxiety）
            assert len(calls) == 2 * n_ticks, \
                f"expect {2 * n_ticks} noise_cap calls, got {len(calls)}"
            lo_raw = calls[0::2]
            anx_raw = calls[1::2]
            # telescoping 恒等：Σ delta_i = x_final − x_0，x_0=0
            assert abs(sum(lo_raw) - st._noise_x["loneliness"]) < 1e-9, \
                f"loneliness 应传增量（telescoping 到最终 OU 状态）：Σ={sum(lo_raw)}, x_N={st._noise_x['loneliness']}"
            assert abs(sum(anx_raw) - st._noise_x["anxiety"]) < 1e-9, \
                f"anxiety 应传增量：Σ={sum(anx_raw)}, x_N={st._noise_x['anxiety']}"
            # 增量有正有负（均值回归，非单调漂移）
            assert any(v < 0 for v in lo_raw), "loneliness 增量应为可正可负（均值回归）"
            assert any(v < 0 for v in anx_raw), "anxiety 增量应为可正可负（均值回归）"
    finally:
        cm.noise_cap = orig_cm
        em_mod.noise_cap = orig_em


def test_noise_loop_total_does_not_scale_with_tick_count():
    """A4 行为守卫：修复后 loop 常驻模式的累积噪声量随 tick 数平稳有界
    （OU 平稳分布，σ=0.3 → 噪声量级 < 1），而非随 tick 数放大。
    固定 seed 下 N=100 与 N=400 的累积原始噪声都远小于 bug 版量级
    （bug 版 Σ完整 x_i ≈ 0.15×N：N=200 即 ~29 漂移；修复版 = x_N ≈ 0.3 级）。"""
    import chiguo_math as cm
    import state.emotion as em_mod
    orig_cm = cm.noise_cap
    orig_em = em_mod.noise_cap

    def run(n_ticks):
        calls = []

        def spy(step_magnitude, raw_noise):
            calls.append(raw_noise)
            return orig_cm(step_magnitude, raw_noise)

        cm.noise_cap = spy
        em_mod.noise_cap = spy
        try:
            with tempfile.TemporaryDirectory() as td:
                st = _make_state(td)
                st.config["emotion"]["noise_enabled"] = 1
                st.config["emotion"]["noise_seed"] = 42
                now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
                st.emotion.loneliness = 60.0
                st.emotion.anxiety = 50.0
                for i in range(n_ticks):
                    st.tick(0.25, now + timedelta(minutes=15 * i))
                return sum(calls[0::2])
        finally:
            cm.noise_cap = orig_cm
            em_mod.noise_cap = orig_em

    s100 = run(100)
    s400 = run(400)
    # 修复版：Σdelta = x_N，|x_N| 为 OU 平稳值（std≈0.3，远小于 3.0）。
    # bug 版：Σx_i ≈ 0.15×N → N=400 时 ~58，必爆。3.0 是 10σ 安全边界。
    assert abs(s100) < 3.0 and abs(s400) < 3.0, \
        f"累积噪声应平稳有界（OU 平稳级），而非随 tick 放大: s100={s100}, s400={s400}"
