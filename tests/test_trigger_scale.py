#!/usr/bin/env python3
"""test_trigger_scale.py — trigger_scale_now + 引擎单点缩放单元测试(批次 3b)"""

import json, os, random, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tomllib
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState, ChiguoEmotion, CooldownState
from chiguo_trigger import evaluate_triggers

CACHE = {"cache_version": 2, "parsed_at": 0, "schedule": {}}  # 空课表:避免课表分支噪音


def make_state(td, cfg, now):
    import re as _re
    src = Path("chiguo_proactive.toml").read_text()
    tmp_toml = Path(td) / "toml.toml"
    src = _re.sub(r"(?m)^mem0_qdrant_path\s*=.*$", f'mem0_qdrant_path = "{Path(td) / "no_qdrant"}"', src)
    tmp_toml.write_text(src)
    with open(tmp_toml, "rb") as f:
        c = tomllib.load(f)
    c["_base_dir"] = td
    c["schedule"].update(cfg.get("schedule", {}))
    s = ChiguoState(c)
    # v10 (#73 A4): 默认孤独 30 → activation ≈ 0.26~0.38 < must_send_activation(0.75)，
    # 保持中段加权竞争语义（special/仪式类不被 must_send 压制）；高段用例显式传 loneliness。
    s.emotion = ChiguoEmotion(loneliness=30.0, affection=55.0, anxiety=40.0, energy=40.0)
    s.cooldown.event_timestamps = []
    s.cooldown.messages_without_reply = 0
    s.cooldown.accumulated_lambda = 0.0
    s.cooldown.held_count = 0
    return s


def _write_plan(td, modifiers):
    Path(td, "schedule_plan.json").write_text(json.dumps(
        {"plan_version": 1, "generated_at": "2026-08-03T15:00:00+08:00", "modifiers": modifiers}))


def _write_overrides(td, items):
    Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": items}))


def test_scale_identity_without_plan():
    """计划缺失/损坏 → 恒等 1.0(全类型缩放为空,窄原语不受影响)"""
    with tempfile.TemporaryDirectory() as td:
        s = make_state(td, {"schedule": {}}, datetime(2026, 8, 5, 14, 0, tzinfo=CST))
        assert s.trigger_scale_now(datetime(2026, 8, 5, 14, 0, tzinfo=CST)) == {}, "无 plan → {}"
        Path(td, "schedule_plan.json").write_text("{broken")
        assert s.trigger_scale_now(datetime(2026, 8, 5, 14, 0, tzinfo=CST)) == {}, "损坏 → {} 恒等"
    print("  OK test_scale_identity_without_plan")


def test_scale_ref_resolution():
    """ref → 日期解析:fact(exam_week 命中)/holiday(命中)/未命中 → 1.0;悬挂 → 跳过+告警"""
    with tempfile.TemporaryDirectory() as td:
        _write_overrides(td, [
            {"id": "e1", "date": "2026-08-03", "end_date": "2026-08-09", "kind": "exam_week",
             "label": "期末", "created_at": "2026-08-01T10:00:00+08:00"},
            {"id": "c1", "date": "2026-08-03", "end_date": "2026-08-07", "kind": "cancel",
             "period": 3, "created_at": "2026-08-01T10:00:00+08:00"}])
        _write_plan(td, [
            {"ref": "fact:e1", "trigger_scale": {"special": 1.0, "lonely_mid": 0.5}},
            {"ref": "holiday:国庆节", "trigger_scale": {"morning": 2.0}},
            {"ref": "fact:c1", "trigger_scale": {"night": 0.1}},     # 区间性 cancel 不可作 ref(N5)
            {"ref": "fact:nope", "trigger_scale": {"meal": 0.1}},    # 悬挂 → 跳过
        ])
        s = make_state(td, {"schedule": {}}, datetime(2026, 8, 5, 14, 0, tzinfo=CST))
        scale = s.trigger_scale_now(datetime(2026, 8, 5, 14, 0, tzinfo=CST))
        assert scale == {"special": 1.0, "lonely_mid": 0.5}, f"国庆未命中/ref 不合格/悬挂均不得入, got {scale}"
        s2 = make_state(td, {"schedule": {}}, datetime(2026, 10, 1, 14, 0, tzinfo=CST))
        scale2 = s2.trigger_scale_now(datetime(2026, 10, 1, 14, 0, tzinfo=CST))
        assert scale2.get("morning") == 2.0, f"国庆当日命中, got {scale2}"
        assert "night" not in scale2 and "meal" not in scale2, "取消条目/悬挂 ref 不得生效"
        # 每 tick 缓存:同日期二次调用不重读
        s._scale_cache["scale"]["special"] = 9.9
        assert s.trigger_scale_now(datetime(2026, 8, 5, 14, 0, tzinfo=CST))["special"] == 9.9, "同日期走缓存"
    print("  OK test_scale_ref_resolution")


def test_engine_scale_loop():
    """缩放循环:候选收集后统一乘;{special: 0.0} → special 永不选中;default 作用于缺席类型"""
    def counts(td, scale, now, loneliness=75.0):
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": [
            {"id": "a1", "type": "anniversary", "name": "认识纪念日", "date": "11-03",
             "note": "", "created_at": "2026-01-01"}]}))
        s = make_state(td, {"schedule": {}}, now)  # 3c:special 源 = anniversary_mgr,toml special_dates 键已废
        s.emotion.loneliness = loneliness  # 对照基线:低孤独 → lonely 候选低于 0.03 截断(同 test_trigger.py:235)
        random.seed(42)
        c = {}
        for _ in range(200):
            t = evaluate_triggers(s, now, trigger_scale=scale)
            c[t.type] = c.get(t.type, 0) + 1
        return c
    now = datetime(2026, 11, 3, 14, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        # 无缩放:special 3.0 高权重且 lonely 候选被截断(实测 200/200,与 test_trigger.py:235 同款)
        c = counts(td, None, now, loneliness=15.0)
        assert c.get("special", 0) == 200, f"无缩放 special 应全中, got {c}"
    with tempfile.TemporaryDirectory() as td:
        # 缩放 {special: 0.0}:权重归零 → 永不选中(类型间相对概率被改变)
        c = counts(td, {"special": 0.0}, now)
        assert c.get("special", 0) == 0, f"special×0.0 后不得再被选中, got {c}"
        assert sum(c.values()) == 200
    print("  OK test_engine_scale_loop")


def test_escape_valve_exempt():
    """逃生阀 longing 溢出在缩放循环前 return → 天然豁免(缩放压不掉破防)"""
    with tempfile.TemporaryDirectory() as td:
        s = make_state(td, {"schedule": {}}, datetime(2026, 8, 5, 14, 0, tzinfo=CST))
        s.emotion.anxiety = 90.0                      # ≥ anxiety_block_threshold(70)
        # 全新 state:last_user_message_at None → silent_hours 999 ≥ 72;last_longing_break_at None → 冷却期外
        s.cooldown.held_count = 5
        t = evaluate_triggers(s, datetime(2026, 8, 5, 14, 0, tzinfo=CST), trigger_scale={"longing": 0.0})
        assert t.type == "longing" and t.data.get("escape_valve"), f"逃生阀必须豁免, got {t}"
    print("  OK test_escape_valve_exempt")


def test_special_source_switch():
    """special 数据源切换(3c):触发器改读 anniversary_mgr 当天匹配,与 T1 同源;
    缩放 {special: 0.5} 作用于新数据源"""
    with tempfile.TemporaryDirectory() as td:
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": [
            {"id": "a1", "type": "anniversary", "name": "认识纪念日", "date": "11-03",
             "note": "", "created_at": "2026-01-01"}]}))
        now = datetime(2026, 11, 3, 14, 0, tzinfo=CST)
        s = make_state(td, {"schedule": {"special_dates": []}}, now)  # toml 键已无 11-03
        random.seed(42)
        c = {}
        for _ in range(100):
            t = evaluate_triggers(s, now, trigger_scale={"special": 0.5})
            c[t.type] = c.get(t.type, 0) + 1
        assert c.get("special", 0) > 0, f"anniversary 当天应触发 special, got {c}"
        assert c.get("special", 0) < 100, f"×0.5 后不得全中, got {c}"
    print("  OK test_special_source_switch")



