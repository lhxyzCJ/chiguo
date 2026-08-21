#!/usr/bin/env python3
"""test_impact_inertia.py — ③ 回复影响惯性阻尼 单元测试（TDD）"""

import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_math import impact_inertia


# ── 纯函数 impact_inertia ─────────────────────────────────────────

def test_inertia_zero_identity():
    """默认 inertia=0 → 恒等（delta 原样返回），灰度先例。"""
    assert impact_inertia(8.0, 0.0, 0.0, 0.0, 50.0) == 8.0
    assert impact_inertia(-3.0, 0.0, 0.0, 0.0, 50.0) == -3.0
    assert impact_inertia(0.0, 0.0, 0.0, 0.0, 50.0) == 0.0


def test_inertia_half_compression():
    """inertia=0.5 → delta × 0.5。"""
    assert abs(impact_inertia(8.0, 0.5, 0.5, 0.0, 50.0) - 4.0) < 1e-9
    assert abs(impact_inertia(8.0, 0.25, 0.25, 0.0, 50.0) - 6.0) < 1e-9


def test_negative_uses_neg_key():
    """负向 delta 走 inertia_neg（独立键，可设更高——lacuna 负向权重更高先例）。"""
    # pos=0 / neg=0.5：正向恒等，负向压缩一半
    assert impact_inertia(3.0, 0.0, 0.5, 0.0, 50.0) == 3.0
    assert abs(impact_inertia(-3.0, 0.0, 0.5, 0.0, 50.0) - (-1.5)) < 1e-9


def test_positive_uses_pos_key():
    """正向 delta 走 inertia（正负键互不干扰）。"""
    assert impact_inertia(3.0, 0.5, 0.0, 0.0, 50.0) == 1.5
    assert impact_inertia(-3.0, 0.5, 0.0, 0.0, 50.0) == -3.0


def test_affection_mod_damps_less_when_closer():
    """affection_mod>0：好感高于 50 时阻尼变小（更易被哄好/更快被伤）。"""
    high_aff = impact_inertia(8.0, 0.5, 0.5, 1.0, 90.0)
    mid_aff = impact_inertia(8.0, 0.5, 0.5, 1.0, 50.0)
    low_aff = impact_inertia(8.0, 0.5, 0.5, 1.0, 10.0)
    assert high_aff > mid_aff > low_aff, \
        f"好感越高阻尼应越小: {high_aff} > {mid_aff} > {low_aff}"


def test_clamp_at_090():
    """inertia_eff 钳制 [0, 0.9]：永不反向、永不归零。"""
    # 0.95 请求 → 实际 0.9 → delta × 0.1
    assert abs(impact_inertia(8.0, 0.95, 0.95, 0.0, 50.0) - 0.8) < 1e-9
    # 负向同样钳制
    assert abs(impact_inertia(-8.0, 0.0, 0.95, 0.0, 50.0) - (-0.8)) < 1e-9


def test_zero_delta():
    """delta=0 → 0（不产生假信号）。"""
    assert impact_inertia(0.0, 0.5, 0.5, 0.5, 80.0) == 0.0


# ── _apply_emotion_impact 行为级（默认恒等 + 开启压缩）──────────────

def _make_state(temp_dir: str):
    """构造临时目录中的 ChiguoState（隔离配置/状态文件）。"""
    import re
    import tomllib
    from chiguo_state import ChiguoState
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(temp_dir)
    return ChiguoState(cfg)


def test_default_toml_identity():
    """默认 toml（impact_inertia_* 全 0）→ 行为与现状逐位一致。

    复刻 test_feedback.py:329-330 的断言值：warmth=1+effort=1+attention=1
    → energy +8.0 / affection +2.5 / tsundere -2.0（无 inertia）。
    """
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        e0, aff0, tsun0, anx0 = (
            st.emotion.energy, st.emotion.affection,
            st.emotion.tsundere_index, st.emotion.anxiety,
        )
        st._apply_emotion_impact(
            {"warmth": 1.0, "effort": 1.0, "attention": 1.0})
        assert abs(st.emotion.energy - (e0 + 8.0)) < 1e-6
        assert abs(st.emotion.affection - (aff0 + 2.5)) < 1e-6
        assert abs(st.emotion.tsundere_index - (tsun0 - 2.0)) < 1e-6
        assert st.emotion.anxiety == anx0  # 正向 warmth 不碰 anxiety


def test_enabled_compresses_delta():
    """显式 cfg（pos=0.5/neg=0.5）→ delta 压缩一半；anxiety 回升同样压缩。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["impact_inertia_positive"] = 0.5
        st.config["emotion"]["impact_inertia_negative"] = 0.5
        st.config["emotion"]["impact_inertia_affection_mod"] = 0.0
        e0, aff0, tsun0 = (
            st.emotion.energy, st.emotion.affection, st.emotion.tsundere_index,
        )
        st._apply_emotion_impact(
            {"warmth": 1.0, "effort": 1.0, "attention": 1.0})
        # energy: warmth*4 + attention*4 = +8 → ×0.5 = +4
        assert abs(st.emotion.energy - (e0 + 4.0)) < 1e-6
        # affection: warmth*1.5 + effort*1.0 = +2.5 → ×0.5 = +1.25
        assert abs(st.emotion.affection - (aff0 + 1.25)) < 1e-6
        # tsundere 软化：-effort*2 = -2.0 → 正向键（效价分桶）→ ×0.5 = -1.0
        assert abs(st.emotion.tsundere_index - (tsun0 - 1.0)) < 1e-6


def test_negative_warmth_compressed_by_neg_key():
    """负 warmth 的 anxiety 回升走 inertia_neg（仅 neg 开启时被压缩）。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["impact_inertia_positive"] = 0.0
        st.config["emotion"]["impact_inertia_negative"] = 0.5
        st.config["emotion"]["impact_inertia_affection_mod"] = 0.0
        anx0 = st.emotion.anxiety
        st._apply_emotion_impact({"warmth": -1.0, "effort": 0.0, "attention": 1.0})
        # anxiety 回升 = 3.0 → ×0.5 = +1.5；affection 负向 = -1.5 → ×0.5 = -0.75
        assert abs(st.emotion.anxiety - (anx0 + 1.5)) < 1e-6


def test_inertia_before_sensitivity_order():
    """顺序锁死：inertia 先压缩 delta → 再走人格 anxiety_sensitivity。

    构造 anxiety_sensitivity=1.3（neuroticism 高），验证最终 anxiety 是
    "压缩后 delta × 1.3"，而非 "原 delta × 1.3 再压缩"。
    """
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["impact_inertia_negative"] = 0.5
        st.config["emotion"]["impact_inertia_affection_mod"] = 0.0
        # 高神经质 → sensitivity = 0.8 + 100/100*0.5 = 1.3
        st.personality.neuroticism = 100.0
        st.personality._cached_anxiety_sensitivity = 1.3
        anx0 = st.emotion.anxiety
        st._anxiety_before_analysis = anx0
        st._apply_emotion_impact({"warmth": -1.0, "effort": 0.0, "attention": 1.0})
        # 原 delta = +3.0 → inertia 压缩 +1.5 → sensitivity ×1.3 = +1.95
        assert abs(st.emotion.anxiety - (anx0 + 1.95)) < 1e-6, \
            f"期望顺序 inertia→sensitivity，实际 {st.emotion.anxiety - anx0}"


def test_tsundere_softening_uses_positive_key():
    """效价分桶：tsundere 软化（delta 为负但语义正向）走正向键，neg 开启不误伤。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["impact_inertia_positive"] = 0.0
        st.config["emotion"]["impact_inertia_negative"] = 0.5
        st.config["emotion"]["impact_inertia_affection_mod"] = 0.0
        tsun0 = st.emotion.tsundere_index
        st._apply_emotion_impact({"warmth": 0.0, "effort": 1.0, "attention": 1.0})
        # 软化 -2.0 走正向键（neg 开启不影响）→ 仍 -2.0
        assert abs(st.emotion.tsundere_index - (tsun0 - 2.0)) < 1e-6


def test_clamp_boundary_with_energy():
    """clamp 交互：energy 接近上限 + inertia 同时存在。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["impact_inertia_positive"] = 0.5
        st.config["emotion"]["impact_inertia_affection_mod"] = 0.0
        st.emotion.energy = 95.0
        st._apply_emotion_impact({"warmth": 1.0, "effort": 0.0, "attention": 1.0})
        # 压缩后 +4 → 99.0（clamp 上限 100 未触达）
        assert abs(st.emotion.energy - 99.0) < 1e-6
