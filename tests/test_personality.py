#!/usr/bin/env python3
"""test_personality.py — 多维人格系统单元测试"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_personality import (
    PersonalityTraits, PersonalityDelta, PersonalityDeltas,
    default_personality, personality_to_dict, personality_from_dict,
)


def test_default_personality():
    """默认人格在合理范围"""
    p = default_personality()
    assert 10 <= p.openness <= 90
    assert 10 <= p.tsundere_intensity <= 90
    assert p.agreeableness > p.neuroticism  # 迟菓本质温柔
    print(f"  OK test_default_personality: tsundere={p.tsundere_intensity:.0f}")


def test_clamp():
    """钳位到 [10, 90]"""
    p = PersonalityTraits(openness=200, extraversion=-50)
    p.clamp()
    assert p.openness == 90.0
    assert p.extraversion == 10.0
    print("  OK test_clamp")


def test_evolve():
    """人格演化累积"""
    p = PersonalityTraits(extraversion=50.0)
    delta = PersonalityDelta(extraversion=0.5, neuroticism=-0.3)
    p.evolve(delta)
    assert p.extraversion == 50.5
    assert p.neuroticism == 60.0 - 0.3  # 默认 60 - 0.3
    print("  OK test_evolve")


def test_evolve_clamp():
    """演化超限自动钳位"""
    p = PersonalityTraits(extraversion=89.5)
    delta = PersonalityDelta(extraversion=1.0)
    p.evolve(delta)
    assert p.extraversion == 90.0  # clamped
    print("  OK test_evolve_clamp")


def test_dominant_profile():
    """人格画像返回正确"""
    p = PersonalityTraits(
        tsundere_intensity=80,  # tsundere_heavy
        extraversion=70,         # extraverted
        neuroticism=70,          # sensitive
        agreeableness=80,        # gentle
        playfulness=70,          # playful
        attachment_style=70,     # anxious_attachment
    )
    profile = p.dominant_profile()
    assert "tsundere_heavy" in profile
    assert "extraverted" in profile
    assert "sensitive" in profile
    assert "gentle" in profile
    assert "playful" in profile
    assert "anxious_attachment" in profile
    print(f"  OK test_dominant_profile: {profile}")


def test_balanced_profile():
    """中间值返回 balanced"""
    p = PersonalityTraits(
        tsundere_intensity=50, extraversion=50, neuroticism=50,
        agreeableness=50, playfulness=50, attachment_style=50,
    )
    profile = p.dominant_profile()
    assert "balanced" in profile
    print(f"  OK test_balanced_profile: {profile}")








def test_anxiety_sensitivity():
    """neuroticism 调制焦虑敏感度"""
    p_low = PersonalityTraits(neuroticism=30)
    p_high = PersonalityTraits(neuroticism=70)
    assert p_low.anxiety_sensitivity() < 1.0
    assert p_high.anxiety_sensitivity() > 1.0
    print(f"  OK test_anxiety_sensitivity: low={p_low.anxiety_sensitivity():.2f} high={p_high.anxiety_sensitivity():.2f}")


def test_openness_bonus():
    """openness 调制话题多样性"""
    p_low = PersonalityTraits(openness=30)
    p_high = PersonalityTraits(openness=70)
    assert p_low.openness_bonus() < 1.5
    assert p_high.openness_bonus() > 1.0
    print(f"  OK test_openness_bonus: low={p_low.openness_bonus():.2f} high={p_high.openness_bonus():.2f}")


def test_serialization_roundtrip():
    """序列化往返"""
    p = PersonalityTraits(
        openness=55, extraversion=45, neuroticism=60,
        tsundere_intensity=70, playfulness=55,
    )
    d = personality_to_dict(p)
    p2 = personality_from_dict(d)
    assert p2.openness == 55
    assert p2.tsundere_intensity == 70
    print("  OK test_serialization_roundtrip")


def test_serialization_missing_fields():
    """缺失字段用默认值填充"""
    d = {"openness": 80}
    p = personality_from_dict(d)
    assert p.openness == 80.0
    assert p.extraversion == 60.0  # default
    print("  OK test_serialization_missing_fields")


def test_evolve_non_contamination():
    """evolve() 只修改指定维度，不污染其他维度"""
    p = PersonalityTraits(
        openness=55.0, conscientiousness=65.0, extraversion=45.0,
        agreeableness=70.0, neuroticism=60.0, tsundere_intensity=70.0,
        playfulness=55.0, attachment_style=60.0,
    )
    delta = PersonalityDelta(extraversion=0.5)
    p.evolve(delta)
    # 修改的维度
    assert p.extraversion == 45.5
    # 其他 7 个维度完全不变
    assert p.openness == 55.0
    assert p.conscientiousness == 65.0
    assert p.agreeableness == 70.0
    assert p.neuroticism == 60.0
    assert p.tsundere_intensity == 70.0
    assert p.playfulness == 55.0
    assert p.attachment_style == 60.0
    print("  OK test_evolve_non_contamination")


def test_personality_delta_constants():
    """预定义变化增量合理"""
    assert -0.2 < PersonalityDeltas.WARM_REPLY.agreeableness < 0.3
    assert -0.2 < PersonalityDeltas.COLD_REPLY.neuroticism < 0.3
    assert PersonalityDeltas.SENT_NO_REPLY.tsundere_intensity > 0  # 无回复 → 傲娇增强
    print("  OK test_personality_delta_constants")
