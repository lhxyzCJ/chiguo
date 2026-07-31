#!/usr/bin/env python3
"""test_personality.py — 多维人格系统单元测试"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def test_tsundere_style_classic():
    """高傲娇 + 低宜人 = classic"""
    p = PersonalityTraits(tsundere_intensity=80, agreeableness=40)
    assert p.tsundere_style() == "tsundere_classic"
    print("  OK test_tsundere_style_classic")


def test_tsundere_style_soft():
    """中傲娇 + 高神经质 = soft"""
    p = PersonalityTraits(tsundere_intensity=60, neuroticism=70)
    assert p.tsundere_style() == "tsundere_soft"
    print("  OK test_tsundere_style_soft")


def test_tsundere_style_dere():
    """低傲娇 + 高依恋 = dere_dere"""
    p = PersonalityTraits(tsundere_intensity=30, attachment_style=70)
    assert p.tsundere_style() == "dere_dere"
    print("  OK test_tsundere_style_dere")


def test_tsundere_style_tsuntsun_maps_to_soft():
    """高傲娇 + 高宜人 = tsuntsun 嘴硬但温柔底子 → tsundere_soft"""
    p = PersonalityTraits(tsundere_intensity=80, agreeableness=70)
    assert p.tsundere_style() == "tsundere_soft"
    print("  OK test_tsundere_style_tsuntsun_maps_to_soft")


def test_tsundere_style_cool_dere_maps_to_cool():
    """低傲娇 + 低依恋 = cool_dere 独立但关心 → tsundere_cool"""
    p = PersonalityTraits(tsundere_intensity=30, attachment_style=40)
    assert p.tsundere_style() == "tsundere_cool"
    print("  OK test_tsundere_style_cool_dere_maps_to_cool")


def test_tsundere_style_valid_cue_keys():
    """tsundere_style 返回值必须是 composer CUES 的合法键"""
    from chiguo_composer import MessageComposer
    cases = [
        PersonalityTraits(tsundere_intensity=80, agreeableness=40),
        PersonalityTraits(tsundere_intensity=80, agreeableness=70),
        PersonalityTraits(tsundere_intensity=60, neuroticism=70),
        PersonalityTraits(tsundere_intensity=60, neuroticism=50),
        PersonalityTraits(tsundere_intensity=30, attachment_style=70),
        PersonalityTraits(tsundere_intensity=30, attachment_style=40),
    ]
    for p in cases:
        assert p.tsundere_style() in MessageComposer.CUES, p.tsundere_style()
    print("  OK test_tsundere_style_valid_cue_keys")


def test_energy_modifier():
    """extraversion 调制元气"""
    p_low = PersonalityTraits(extraversion=30)
    p_high = PersonalityTraits(extraversion=70)
    assert p_low.energy_modifier() < 1.0
    assert p_high.energy_modifier() > 1.0
    print(f"  OK test_energy_modifier: low={p_low.energy_modifier():.2f} high={p_high.energy_modifier():.2f}")


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
    assert p.extraversion == 45.0  # default
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


if __name__ == "__main__":
    print("test_personality.py\n")
    tests = [
        test_default_personality,
        test_clamp,
        test_evolve,
        test_evolve_clamp,
        test_dominant_profile,
        test_balanced_profile,
        test_tsundere_style_classic,
        test_tsundere_style_soft,
        test_tsundere_style_dere,
        test_tsundere_style_tsuntsun_maps_to_soft,
        test_tsundere_style_cool_dere_maps_to_cool,
        test_tsundere_style_valid_cue_keys,
        test_energy_modifier,
        test_anxiety_sensitivity,
        test_openness_bonus,
        test_serialization_roundtrip,
        test_serialization_missing_fields,
        test_evolve_non_contamination,
        test_personality_delta_constants,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} personality tests passed.")
