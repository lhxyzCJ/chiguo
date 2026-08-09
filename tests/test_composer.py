#!/usr/bin/env python3
"""test_composer.py — 消息组合系统单元测试"""

import sys, os
import random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

random.seed(42)  # 固定种子 → 概率断言确定性（同 test_topics.py 做法）

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from chiguo_composer import MessageComposer


class MockState:
    """最小化 mock ChiguoState，只提供 composer 需要的接口"""
    def __init__(self, personality=None):
        self._personality = personality
        self._exam_ranges = []

    @property
    def personality(self):
        return self._personality

    @property
    def exam_ranges(self):
        return self._exam_ranges

    def schedule_status(self, now):
        return {"in_class": False, "class_load": "free", "remaining_classes": 0}

    def holiday_parser(self):
        class Mock:
            def is_holiday(self, now): return False
        return Mock()
    holiday_parser = property(holiday_parser)


def make_composer(personality=None):
    from chiguo_personality import PersonalityTraits
    if personality is None:
        personality = PersonalityTraits()
    state = MockState(personality)
    return MessageComposer(state, {})


def test_select_combo_returns_valid():
    """combo 返回有效结构"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    combo = c.select_combo("lonely_low", now)
    assert "size" in combo
    assert combo["size"] in (1, 2, 3)
    assert "intent" in combo
    assert "combo_string" in combo
    assert combo["intent"]["text"]  # 非空
    print(f"  OK test_select_combo_returns_valid: size={combo['size']}, combo={combo['combo_string']}")


def test_combo_size_distribution():
    """combo 尺寸分布概率合理"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    sizes = {1: 0, 2: 0, 3: 0}
    for _ in range(200):
        combo = c.select_combo("lonely_mid", now)
        sizes[combo["size"]] += 1
    # size 2 (0.50) 应该最多
    assert sizes[2] > 50  # 200 * 0.50 = 100
    print(f"  OK test_combo_size_distribution: {sizes}")


def test_different_triggers_different_intents():
    """不同触发类型产生不同 intent"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

    combo_low = c.select_combo("lonely_low", now)
    combo_high = c.select_combo("lonely_high", now)
    combo_playful = c.select_combo("playful", now)

    # lonely_high 的 intent 应该更脆弱/真诚
    high_text = combo_high["intent"]["text"]
    assert any(w in high_text for w in ["崩溃", "爆发", "想念", "脆弱", "请求"])
    # playful 的 intent 应该更活泼
    playful_text = combo_playful["intent"]["text"]
    assert any(w in playful_text for w in ["趣事", "调皮", "分享", "日常", "无厘头"])
    print(f"  OK test_different_triggers_different_intents")


def test_cue_present_for_size_2plus():
    """size ≥ 2 时有 Cue"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

    sizes_with_cue = 0
    total_2plus = 0
    for _ in range(100):
        combo = c.select_combo("lonely_low", now)
        if combo["size"] >= 2:
            total_2plus += 1
            if combo.get("cue"):
                sizes_with_cue += 1
    assert sizes_with_cue == total_2plus  # 所有 size≥2 都应有 cue
    print(f"  OK test_cue_present_for_size_2plus: {sizes_with_cue}/{total_2plus}")


def test_cue_modulated_by_personality():
    """傲娇人格调制 Cue 权重"""
    from chiguo_personality import PersonalityTraits

    tsun_high = PersonalityTraits(tsundere_intensity=85)
    tsun_low = PersonalityTraits(tsundere_intensity=25)

    c_high = make_composer(tsun_high)
    c_low = make_composer(tsun_low)

    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

    # 调制度权重
    w_high = c_high._modulate_cue_weights("lonely_low")
    w_low = c_low._modulate_cue_weights("lonely_low")

    # 高傲娇 → tsundere_classic 权重更高
    assert w_high["tsundere_classic"] > w_low["tsundere_classic"]
    # 低傲娇 → dere_dere 权重更高
    assert w_low["dere_dere"] > w_high["dere_dere"]
    print("  OK test_cue_modulated_by_personality")


def test_trigger_modulates_cue():
    """触发类型调制 Cue 权重"""
    c = make_composer()
    w_lonely_high = c._modulate_cue_weights("lonely_high")
    w_playful = c._modulate_cue_weights("playful")

    # lonely_high → anxious 权重大幅提高（基础权重 ×2.0，Task 6 调低基础后改用相对断言）
    assert w_lonely_high["anxious_clingy"] >= 2.0 * c.cue_weights["anxious_clingy"]
    # playful → playful_bubbly 权重极高（基础权重 ×3.0）
    assert w_playful["playful_bubbly"] >= 3.0 * c.cue_weights["playful_bubbly"]
    print("  OK test_trigger_modulates_cue")


def test_vibe_selection():
    """Vibe 按时间选择正确"""
    c = make_composer()

    morning = c._select_vibe(datetime(2026, 6, 15, 8, 0, tzinfo=CST))
    noon = c._select_vibe(datetime(2026, 6, 15, 13, 0, tzinfo=CST))
    night = c._select_vibe(datetime(2026, 6, 15, 22, 0, tzinfo=CST))

    assert morning and ("早晨" in morning or "early" in morning.lower() or "清新" in morning)
    assert noon and ("午间" in noon or "noon" in noon.lower() or "慵懒" in noon)
    assert night and ("night" in night.lower() or "晚上" in night or "深夜" in night or "夜晚" in night)
    print(f"  OK test_vibe_selection: {morning} / {noon} / {night}")


def test_compose_situation():
    """情境组合输出非空且有结构"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    combo = c.select_combo("lonely_mid", now)

    from chiguo_trigger import Trigger
    trigger = Trigger(type="lonely_mid")

    situation = c.compose_situation(combo, None, 5.0)
    assert len(situation) > 20
    assert "哥哥" in situation or "主人" in situation or "风格" in situation or "氛围" in situation
    print(f"  OK test_compose_situation: {len(situation)} chars")


def test_all_trigger_types():
    """所有触发类型都有 intent"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

    for ttype in MessageComposer.INTENTS:
        combo = c.select_combo(ttype, now)
        assert combo["intent"]["text"], f"no intent for {ttype}"
    print(f"  OK test_all_trigger_types: {len(MessageComposer.INTENTS)} trigger types")


def test_reflect_trigger_has_intent():
    """reflect 触发有 intent"""
    c = make_composer()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    combo = c.select_combo("reflect", now)
    assert combo["intent"]
    assert any(w in combo["intent"]["text"] for w in ["内省", "变化", "成长", "温柔"])
    print(f"  OK test_reflect_trigger_has_intent: {combo['intent']['text'][:60]}")


if __name__ == "__main__":
    print("test_composer.py\n")
    tests = [
        test_select_combo_returns_valid,
        test_combo_size_distribution,
        test_different_triggers_different_intents,
        test_cue_present_for_size_2plus,
        test_cue_modulated_by_personality,
        test_trigger_modulates_cue,
        test_vibe_selection,
        test_compose_situation,
        test_all_trigger_types,
        test_reflect_trigger_has_intent,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} composer tests passed.")
