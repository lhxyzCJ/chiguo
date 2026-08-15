#!/usr/bin/env python3
"""test_trigger_types.py — 触发类型枚举一致性 + comfort replan 验收 (T7·Q3 #265)"""

import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trigger_types import (TriggerType, EMOTION_TRIGGERS, RITUAL_TRIGGERS,
                           TRIGGER_TYPE_VALUES, REPLAN_SCALE_KEYS)
from schedule.replan import TRIGGER_TYPES, validate_plan, sanitize_plan

CFG = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04"}}


def test_enum_consistency_trigger_subset_replan():
    """验收标准①：trigger 产出集合 ⊆ replan 集合（comfort 缺失是 Q3 缺陷）。
    replan 合法 scale key = 全部真实触发类型 + default；每个 TriggerType 值都必须在内。"""
    assert set(TriggerType) <= REPLAN_SCALE_KEYS, (
        f"触发集合非 replan 子集,缺: {set(TriggerType) - REPLAN_SCALE_KEYS}")
    assert TriggerType.COMFORT.value in REPLAN_SCALE_KEYS, "comfort 必须被 replan 接受"
    # replan TRIGGER_TYPES 由枚举派生,应含 comfort
    assert "comfort" in TRIGGER_TYPES, f"replan TRIGGER_TYPES 缺 comfort, got {TRIGGER_TYPES}"
    print("  OK test_enum_consistency_trigger_subset_replan")


def test_enum_partition():
    """情绪类 ∪ 仪式类 == 全部真实触发类型,且互斥;comfort 归情绪类。"""
    assert EMOTION_TRIGGERS | RITUAL_TRIGGERS == set(TriggerType), \
        "情绪/仪式分区未覆盖全枚举"
    assert EMOTION_TRIGGERS.isdisjoint(RITUAL_TRIGGERS), "分区重叠"
    assert TriggerType.COMFORT in EMOTION_TRIGGERS, "comfort 应属情绪类"
    assert TriggerType.LONELY_LOW in EMOTION_TRIGGERS, "lonely_low 应属情绪类"
    assert TriggerType.MORNING in RITUAL_TRIGGERS, "morning 应属仪式类"
    print("  OK test_enum_partition")


def test_value_set_matches_enum():
    """TRIGGER_TYPE_VALUES 与 TriggerType 值一一对应(单一来源一致性)。"""
    assert TRIGGER_TYPE_VALUES == {t.value for t in TriggerType}
    assert len(TRIGGER_TYPE_VALUES) == len(TriggerType), "枚举值重复"
    print("  OK test_value_set_matches_enum")


def test_comfort_accepted_by_replan():
    """comfort 场景端到端:replan 校验接受含 comfort 的 trigger_scale(不再剔/拒)。"""
    with tempfile.TemporaryDirectory() as td:
        from schedule.sources import load_sources
        src = load_sources(td, CFG)
        plan = {"modifiers": [{"ref": "holiday:国庆节", "trigger_scale": {"comfort": 1.5}}]}
        errs = validate_plan(plan, src)
        assert not errs, f"含 comfort 的 plan 校验不应报错, got {errs}"
    print("  OK test_comfort_accepted_by_replan")


def test_sanitize_plan_prunes_invalid_keeps_valid():
    """验收标准③：校验失败降级为剔非法 key/条目,保留合法部分。"""
    with tempfile.TemporaryDirectory() as td:
        from schedule.sources import load_sources
        src = load_sources(td, CFG)
        plan = {"modifiers": [
            {"ref": "holiday:国庆节", "trigger_scale": {"comfort": 1.5, "xxx": 2.0, "special": 20.0}},
            {"ref": "holiday:春节", "trigger_scale": {"morning": 0.5}},
            {"ref": "fact:bad", "trigger_scale": {"night": 1.0}},     # 非法 ref → 整条剔
            {"ref": "holiday:国庆节", "trigger_scale": {"meal": 1.0}, "hack": 1},  # 未知字段 → 整条剔
        ]}
        kept, warns = sanitize_plan(plan, src)
        assert len(kept) == 2, f"应保留 2 条合法 modifier, got {len(kept)}: {kept}"
        assert kept[0]["ref"] == "holiday:国庆节", "国庆合法保留"
        assert "xxx" not in kept[0]["trigger_scale"], "未知类型名被剔"
        assert "special" not in kept[0]["trigger_scale"], "clamp 越界被剔"
        assert kept[0]["trigger_scale"]["comfort"] == 1.5, "comfort 合法 key 保留"
        assert len(warns) >= 4, f"应有告警(未登录剔除), got {len(warns)}: {warns}"
    print("  OK test_sanitize_plan_prunes_invalid_keeps_valid")
