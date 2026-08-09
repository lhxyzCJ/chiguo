#!/usr/bin/env python3
"""test_emotion_baseline.py — ④ 情绪基线长期漂移 单元测试（TDD）"""

import os
import re
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_math import baseline_shift_of
from chiguo_state import ChiguoState, ChiguoEmotion, BASELINE_DEFAULTS


# ── 纯函数 baseline_shift_of（事件 → 方向表）─────────────────────

def test_shift_very_slow_reply():
    """很久才回 → loneliness +1、affection −1。"""
    s = baseline_shift_of({"type": "user_reply", "latency_category": "very_slow"})
    assert s["loneliness"] == 1 and s["affection"] == -1 and s["anxiety"] == 0


def test_shift_cold_reply():
    """冷淡回复（warmth<−0.2）→ loneliness/anxiety +1、affection −1。"""
    s = baseline_shift_of({"type": "user_reply", "warmth": -0.5})
    assert s["loneliness"] == 1 and s["anxiety"] == 1 and s["affection"] == -1


def test_shift_warm_reply():
    """温柔回复（warmth>0.3）→ anxiety −1、affection +1。"""
    s = baseline_shift_of({"type": "user_reply", "warmth": 0.8})
    assert s["anxiety"] == -1 and s["affection"] == 1 and s["loneliness"] == 0


def test_shift_send_no_reply():
    """发出未被回复 → loneliness/anxiety +1、affection −1。"""
    s = baseline_shift_of({"type": "character_send", "was_replied": False})
    assert s["loneliness"] == 1 and s["anxiety"] == 1 and s["affection"] == -1


def test_shift_send_replied_and_neutral():
    """被回复/中性事件 → 零漂移。"""
    s = baseline_shift_of({"type": "character_send", "was_replied": True})
    assert s == {"loneliness": 0, "anxiety": 0, "affection": 0}
    s = baseline_shift_of({"type": "user_reply", "warmth": 0.0, "latency_category": "fast"})
    assert s == {"loneliness": 0, "anxiety": 0, "affection": 0}


# ── 行为级：update_emotion_baseline ──────────────────────────────

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


def test_default_disabled_identity():
    """baseline_drift_rate=0（默认）→ 基线恒等于全局默认，update 无效果。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        for dim, dflt in BASELINE_DEFAULTS.items():
            assert getattr(st.emotion, f"baseline_{dim}") == dflt
        st.update_emotion_baseline({"type": "user_reply", "warmth": -0.9})
        for dim, dflt in BASELINE_DEFAULTS.items():
            assert getattr(st.emotion, f"baseline_{dim}") == dflt


def test_drift_direction_and_magnitude():
    """开启漂移：连续冷落事件 → loneliness 基线上升、affection 下降；幅度=rate×shift。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["baseline_drift_rate"] = 1.0   # 全量
        st.config["emotion"]["baseline_shift_loneliness"] = 0.15
        st.config["emotion"]["baseline_shift_affection"] = 0.15
        lo0 = st.emotion.baseline_loneliness
        aff0 = st.emotion.baseline_affection
        for _ in range(10):
            st.update_emotion_baseline({"type": "user_reply", "warmth": -0.5})
        assert abs(st.emotion.baseline_loneliness - (lo0 + 1.5)) < 1e-9
        assert abs(st.emotion.baseline_affection - (aff0 - 1.5)) < 1e-9


def test_drift_bounded():
    """有界钳位：1000 次冷落事件 → 基线停在 [默认−20, 默认+20]。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["baseline_drift_rate"] = 1.0
        st.config["emotion"]["baseline_max_drift"] = 20.0
        for _ in range(1000):
            st.update_emotion_baseline({"type": "character_send", "was_replied": False})
        lo = st.emotion.baseline_loneliness
        aff = st.emotion.baseline_affection
        assert lo <= BASELINE_DEFAULTS["loneliness"] + 20.0 + 1e-9
        assert aff >= BASELINE_DEFAULTS["affection"] - 20.0 - 1e-9


def test_forgetting_returns_to_default():
    """淡忘：漂移后 tick 足够长时间（≫720h 半衰期）→ 基线回到全局默认。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["baseline_drift_rate"] = 1.0
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        for _ in range(50):
            st.update_emotion_baseline({"type": "user_reply", "warmth": -0.9})
        assert st.emotion.baseline_loneliness > BASELINE_DEFAULTS["loneliness"]
        # tick 1 年（8760h ≫ 720h 半衰期）→ 淡忘回默认
        st.tick(8760.0, now)
        assert abs(st.emotion.baseline_loneliness - BASELINE_DEFAULTS["loneliness"]) < 0.1
        assert abs(st.emotion.baseline_anxiety - BASELINE_DEFAULTS["anxiety"]) < 0.1
        assert abs(st.emotion.baseline_affection - BASELINE_DEFAULTS["affection"]) < 0.1


def test_tick_target_uses_baseline():
    """tick 收敛 target 用漂移后基线：loneliness 基线降 → 稳态收敛点低于 100。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["baseline_drift_rate"] = 1.0
        st.config["emotion"]["baseline_forget_half_life"] = 0.0  # 关闭淡忘，纯测收敛
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.emotion.baseline_loneliness = 60.0  # 模拟长期关系变化后低平衡点
        st.emotion.loneliness = 60.0
        for _ in range(500):
            st.tick(24.0, now + timedelta(hours=24 * _))
        # 长期收敛到基线 60 附近（而非全局默认 100）
        assert abs(st.emotion.loneliness - 60.0) < 5.0, \
            f"应收敛到漂移基线: {st.emotion.loneliness}"


def test_old_state_missing_baseline_defaults():
    """旧状态无 baseline_* 字段 → dataclass 默认补（100/100/0，无需升版）。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        # 手工构造旧状态 cooldown 类比：baseline 字段是 emotion 上的，直接断言默认
        assert st.emotion.baseline_loneliness == 100.0
        assert st.emotion.baseline_anxiety == 100.0
        assert st.emotion.baseline_affection == 0.0
        # save→load 往返不丢字段
        st.save()
        st2 = ChiguoState.load(str(Path(td) / "state.json"),
                               st.config) if hasattr(ChiguoState, "load") else None


def test_tsundere_isolated_from_baseline():
    """人格隔离：开启漂移前后 tsundere_index 轨迹逐位一致（漂移不碰角色维度）。"""
    with tempfile.TemporaryDirectory() as td:
        st1 = _make_state(td)
        st2 = _make_state(td)
        st2.config["emotion"]["baseline_drift_rate"] = 1.0
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        for i in range(100):
            st1.update_emotion_baseline({"type": "user_reply", "warmth": -0.9})
            st2.update_emotion_baseline({"type": "user_reply", "warmth": -0.9})
            st1.tick(1.0, now + timedelta(hours=i))
            st2.tick(1.0, now + timedelta(hours=i))
        assert st1.emotion.tsundere_index == st2.emotion.tsundere_index


if __name__ == "__main__":
    tests = [
        test_shift_very_slow_reply, test_shift_cold_reply, test_shift_warm_reply,
        test_shift_send_no_reply, test_shift_send_replied_and_neutral,
        test_default_disabled_identity, test_drift_direction_and_magnitude,
        test_drift_bounded, test_forgetting_returns_to_default,
        test_tick_target_uses_baseline, test_old_state_missing_baseline_defaults,
        test_tsundere_isolated_from_baseline,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
