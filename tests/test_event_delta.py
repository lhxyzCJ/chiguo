#!/usr/bin/env python3
"""test_event_delta.py — B1 事件类型化情绪 delta 单元测试

覆盖: 默认关闭恒等 / EVENT_DELTA 规则表逐事件命中 / 事件类型宽松提取
（显式 event_type 键 + warmth/user_mood/topic 信号推断）/ 中文别名映射 /
未知事件零效果 / 经 _apply_analysis_impact 端到端生效（先于 inertia 直接加减）。
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import tempfile
import tomllib
from pathlib import Path

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState, EVENT_DELTA


def _make_state(temp_dir: str) -> ChiguoState:
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(temp_dir)
    return ChiguoState(cfg)


def test_default_off_identity():
    """event_delta_enabled 默认 False → apply_event_delta 零效果（恒等）。"""
    s = _make_state(tempfile.mkdtemp())
    s.emotion.loneliness = 50.0
    s.emotion.affection = 20.0
    s.emotion.anxiety = 40.0
    s.apply_event_delta("praise")
    assert s.emotion.loneliness == 50.0 and s.emotion.affection == 20.0
    print("  OK test_default_off_identity")


def test_event_delta_table_rule_hits():
    """开启后：规则表各事件命中 → 直接加减情绪。"""
    s = _make_state(tempfile.mkdtemp())
    s.config["emotion"]["event_delta_enabled"] = True
    cases = [
        ("praise",        {"loneliness": -3.0, "affection": 2.0}),
        ("criticism",     {"loneliness": 2.0, "anxiety": 3.0}),
        ("contradiction", {"anxiety": 4.0}),
        ("comfort",       {"anxiety": -3.0, "affection": 1.5}),
        ("new_topic",     {"affection": 1.0}),
        ("question",      {"affection": 0.8}),
        ("complaint",     {"anxiety": 2.0}),
    ]
    for evt, delta in cases:
        s.emotion.loneliness = 50.0
        s.emotion.affection = 20.0
        s.emotion.anxiety = 40.0
        s.apply_event_delta(evt)
        for dim, d in delta.items():
            assert abs(getattr(s.emotion, dim) - (50.0 if dim == "loneliness"
                       else 20.0 if dim == "affection" else 40.0) - d) < 1e-9, \
                f"{evt} 的 {dim} delta 未生效"
    print("  OK test_event_delta_table_rule_hits")


def test_extract_event_type_signals():
    """analysis 信号推断：warmth/user_mood/topic → 事件类型。"""
    s = _make_state(tempfile.mkdtemp())
    assert s._extract_event_type({"warmth": 0.8}) == "praise"
    assert s._extract_event_type({"warmth": -0.5}) == "criticism"
    assert s._extract_event_type({"user_mood": "low"}) == "comfort"
    assert s._extract_event_type({"user_mood": "distressed"}) == "comfort"
    assert s._extract_event_type({"topic": "周末去公园"}) == "new_topic"
    assert s._extract_event_type({"warmth": 0.0}) is None
    assert s._extract_event_type("notadict") is None
    print("  OK test_extract_event_type_signals")


def test_extract_event_type_explicit_key():
    """显式 event_type/event 键优先于信号推断。"""
    s = _make_state(tempfile.mkdtemp())
    assert s._extract_event_type({"event_type": "comfort", "warmth": 0.9}) == "comfort"
    assert s._extract_event_type({"event": "question"}) == "question"
    print("  OK test_extract_event_type_explicit_key")


def test_synonym_mapping():
    """中文别名 + 标点 → 规范事件类型（宽松匹配）。"""
    s = _make_state(tempfile.mkdtemp())
    s.config["emotion"]["event_delta_enabled"] = True
    s.emotion.anxiety = 40.0
    s.apply_event_delta("批评！")
    assert abs(s.emotion.anxiety - 43.0) < 1e-9, "批评 → criticism（anxiety +3）"
    s.emotion.loneliness = 50.0
    s.apply_event_delta("安慰一下")
    assert s.emotion.loneliness == 50.0  # 安慰不影响 loneliness
    print("  OK test_synonym_mapping")


def test_unknown_event_noop():
    """未知事件类型 → 零效果。"""
    s = _make_state(tempfile.mkdtemp())
    s.config["emotion"]["event_delta_enabled"] = True
    s.emotion.loneliness = 50.0
    s.emotion.affection = 20.0
    s.emotion.anxiety = 40.0
    s.apply_event_delta("some_random_event")
    s.apply_event_delta(None)
    assert s.emotion.loneliness == 50.0 and s.emotion.affection == 20.0 and s.emotion.anxiety == 40.0
    print("  OK test_unknown_event_noop")


def test_apply_analysis_impact_wired():
    """_apply_analysis_impact 端到端：开启后 analysis 携带事件 → 事件 delta 叠加生效
    （与普通情绪影响并存，差值恰为规则表 delta）；关闭 → 差值 0（恒等）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        analysis = {"event_type": "praise", "warmth": 0.9}
        # 关闭（基线）：仅普通情绪影响
        s_off = _make_state(td)
        s_off.emotion.loneliness = 50.0
        s_off.emotion.affection = 20.0
        s_off.emotion.anxiety = 40.0
        s_off._apply_analysis_impact(dict(analysis), now)
        base = (s_off.emotion.loneliness, s_off.emotion.affection)
        # 开启：事件 delta（praise → loneliness-3 / affection+2）叠加于基线之上
        s_on = _make_state(td)
        s_on.config["emotion"]["event_delta_enabled"] = True
        s_on.emotion.loneliness = 50.0
        s_on.emotion.affection = 20.0
        s_on.emotion.anxiety = 40.0
        s_on._apply_analysis_impact(dict(analysis), now)
        assert abs(s_on.emotion.loneliness - base[0] + 3.0) < 1e-9, \
            f"praise 事件 delta loneliness-3 未叠加: {base[0]} → {s_on.emotion.loneliness}"
        assert abs(s_on.emotion.affection - base[1] - 2.0) < 1e-9, \
            f"praise 事件 delta affection+2 未叠加: {base[1]} → {s_on.emotion.affection}"
    print("  OK test_apply_analysis_impact_wired")



