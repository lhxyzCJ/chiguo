#!/usr/bin/env python3
"""test_context_composer_rules.py — Issue #400 表驱动重构回归测试

锁定重构前后逐字节等价的行为：
- decision.context: _guidance_for_layer / _energy_note / _urgency_note / _safety_note
  阈值边界与原文案（含孤独优先于不安、未知层→空串）
- chiguo_composer: VIBE_HOURS 24 小时全映射 + 周末调制、
  TRIGGER_RULES 未知触发→基线权重不变、COMFORT 安抚调制保留。
"""

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from decision.context import (
    _guidance_for_layer, _energy_note, _urgency_note, _safety_note,
)
from chiguo_composer import MessageComposer


# ── _guidance_for_layer ──────────────────────────────

def test_guidance_known_layers_nonempty():
    for layer in ("shell", "middle", "kernel"):
        assert _guidance_for_layer(layer), layer
    print("  OK test_guidance_known_layers_nonempty")


def test_guidance_unknown_layer_empty():
    assert _guidance_for_layer("nope") == ""
    print("  OK test_guidance_unknown_layer_empty")


# ── _energy_note：边界 <20 / <40 / >80 ────────────────

def test_energy_note_tiers():
    assert "冷淡" in _energy_note(10.0)
    assert "克制" in _energy_note(20.0)  # 恰 20 → 第二档（原 <20 不命中）
    assert "克制" in _energy_note(30.0)
    assert _energy_note(40.0) == ""      # 恰 40 → 空（原 <40 不命中）
    assert _energy_note(50.0) == ""
    assert _energy_note(80.0) == ""      # 恰 80 → 空（原 >80 不命中）
    assert "充沛" in _energy_note(90.0)
    print("  OK test_energy_note_tiers")


# ── _urgency_note：孤独优先，默认阈值 3.0 / 2.0 ───────

def test_urgency_note_loneliness_first():
    note = _urgency_note(5.0, 9.0, {})
    assert "孤独" in note and "不安" not in note
    print("  OK test_urgency_note_loneliness_first")


def test_urgency_note_anxiety_only():
    note = _urgency_note(1.0, 5.0, {})
    assert "不安" in note
    print("  OK test_urgency_note_anxiety_only")


def test_urgency_note_none_when_calm():
    assert _urgency_note(1.0, 1.0, {}) == ""
    print("  OK test_urgency_note_none_when_calm")


# ── _safety_note：破防 + 安全阀等级叠加 ───────────────

def _trig(**data):
    return SimpleNamespace(data=data)


def test_safety_note_levels():
    assert _safety_note(_trig(), 0) == ""
    assert "24h" in _safety_note(_trig(), 1)
    assert "48h" in _safety_note(_trig(), 2)
    both = _safety_note(_trig(escape_valve=True), 2)
    assert "破防" in both and "48h" in both
    assert "破防" in _safety_note(_trig(escape_valve=True), 0)
    print("  OK test_safety_note_levels")


# ── VIBE_HOURS：24 小时全映射（原 6≤h<9…else late_night）──

def _composer():
    from chiguo_personality import PersonalityTraits
    return MessageComposer(SimpleNamespace(personality=PersonalityTraits()), {})


def test_vibe_hours_full_day():
    c = _composer()
    expected = {
        0: "late_night", 5: "late_night", 6: "early_morning", 8: "early_morning",
        9: "morning", 11: "morning", 12: "noon", 13: "noon",
        14: "afternoon", 17: "afternoon", 18: "evening", 20: "evening",
        21: "night", 22: "night", 23: "late_night",
    }
    # 2026-06-15 是周一（wd=0），无周末调制
    for h, base in expected.items():
        vibe = c._select_vibe(datetime(2026, 6, 15, h, 0, tzinfo=CST))
        assert base in c.VIBES and vibe == c.VIBES[base], (h, vibe)
    print("  OK test_vibe_hours_full_day")


def test_vibe_weekend_modulation():
    c = _composer()
    # 2026-06-20 是周六（wd=5）
    assert c._select_vibe(datetime(2026, 6, 20, 8, 0, tzinfo=CST)) == c.VIBES["weekend_morning"]
    assert c._select_vibe(datetime(2026, 6, 20, 22, 0, tzinfo=CST)) == c.VIBES["weekend_evening"]
    # 周末午间不受调制
    assert c._select_vibe(datetime(2026, 6, 20, 13, 0, tzinfo=CST)) == c.VIBES["noon"]
    print("  OK test_vibe_weekend_modulation")


# ── TRIGGER_RULES：未知触发→基线不变，COMFORT 调制保留 ──

def test_trigger_rules_unknown_is_baseline():
    from chiguo_personality import PersonalityTraits
    # 中性人格（全档位区间内）+ 未知触发 → 各权重 == 基线
    p = PersonalityTraits(tsundere_intensity=50, extraversion=50,
                          neuroticism=50, agreeableness=50)
    c = MessageComposer(SimpleNamespace(personality=p), {})
    w = c._modulate_cue_weights("some_unknown_trigger")
    assert w == c.cue_weights, w
    print("  OK test_trigger_rules_unknown_is_baseline")


def test_trigger_rules_comfort():
    c = _composer()
    w = c._modulate_cue_weights("comfort")
    assert w["caring_gentle"] > c.cue_weights["caring_gentle"]
    assert w["tsundere_classic"] < c.cue_weights["tsundere_classic"]
    print("  OK test_trigger_rules_comfort")
