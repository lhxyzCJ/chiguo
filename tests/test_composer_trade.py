#!/usr/bin/env python3
"""test_composer_trade.py — cue 权重重排 + trade_tsundere 测试（Phase 2 Task 6）

原著对齐：经典傲娇为最高权重（嘴硬心软为主）、温柔关心降权（关心必带刺）、
禁止嘻嘻高频（全书仅 10 次）、新增交易式撒娇 cue（原著核心交易思维）。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tomllib

from chiguo_composer import MessageComposer
from chiguo_personality import PersonalityTraits


class MockState:
    """与 test_composer.py 一致的最小 mock ChiguoState"""
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


def make_composer():
    """cue_weights 在 __init__ 中从 config 读（测试传 {} → 走代码默认值）"""
    state = MockState(PersonalityTraits())
    return MessageComposer(state, {})


def test_cue_weights_rebalanced():
    """经典傲娇应高于温柔关心；trade_tsundere 存在且有权重；cool 保留但权重 0"""
    c = make_composer()
    w = c.cue_weights
    assert w.get("tsundere_classic", 0) >= w.get("caring_gentle", 1), "经典傲娇应高于温柔关心"
    assert "trade_tsundere" in c.CUES, "应有交易式撒娇 cue"
    assert w.get("trade_tsundere", 0) > 0, "trade_tsundere 应有基础权重"
    assert "cool_mysterious" not in c.CUES, "cool_mysterious 不可达，应已删除"
    print("  OK test_cue_weights_rebalanced")


def test_toml_composer_weights():
    """toml [composer] 段权重与代码默认一致且含 cue_trade_weight（daemon 实际读这里）"""
    toml_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "chiguo_proactive.toml")
    with open(toml_path, "rb") as f:
        cfg = tomllib.load(f)
    comp = cfg["composer"]
    assert "cue_trade_weight" in comp, "toml 应新增 cue_trade_weight 键"
    assert comp["cue_tsundere_weight"] >= comp["cue_caring_weight"], "经典傲娇应高于温柔关心"
    assert comp["cue_cool_weight"] == 0.00, "cool 权重应为 0"
    assert comp["cue_trade_weight"] == 0.15
    print("  OK test_toml_composer_weights")


def test_playful_no_xi_xi():
    """playful 不应鼓励嘻嘻高频"""
    hint = MessageComposer.CUES["playful_bubbly"]["style_hint"]
    assert "嘻嘻" not in hint, "playful 不应鼓励嘻嘻高频"
    print("  OK test_playful_no_xi_xi")


def test_trade_tsundere_style():
    """trade_tsundere 用交易/补偿框架包装"""
    cue = MessageComposer.CUES["trade_tsundere"]
    assert "交易" in cue["description"], "描述应有交易式撒娇"
    assert "补偿" in cue["style_hint"], "风格应有补偿方案"
    print("  OK test_trade_tsundere_style")


def test_morning_no_caring_boost():
    """morning/meal/night 不再给 caring ×1.5（关心必带刺，不需要单独放大）"""
    c = make_composer()
    for tt in ("morning", "meal", "night"):
        w = c._modulate_cue_weights(tt)
        assert w["caring_gentle"] == c.cue_weights["caring_gentle"], f"{tt} 不应放大 caring"
    print("  OK test_morning_no_caring_boost")


if __name__ == "__main__":
    print("test_composer_trade.py\n")
    tests = [
        test_cue_weights_rebalanced,
        test_toml_composer_weights,
        test_playful_no_xi_xi,
        test_trade_tsundere_style,
        test_morning_no_caring_boost,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} composer-trade tests passed.")
