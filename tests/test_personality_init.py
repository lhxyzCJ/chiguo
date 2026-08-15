#!/usr/bin/env python3
"""test_personality_init.py — 初始人格值对齐原著测试（Task 5）"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_personality import default_personality


def test_default_style_is_classic_tsundere():
    p = default_personality()
    assert p.tsundere_intensity >= 70, f"初始傲娇应 ≥70，实际 {p.tsundere_intensity:.1f}"


def test_default_traits_reasonable():
    p = default_personality()
    assert p.extraversion >= 55, "外向性应体现小太阳人设"
    assert p.agreeableness <= 68, "宜人性不应过高（毒舌但善良）"
    assert p.tsundere_intensity >= 70



