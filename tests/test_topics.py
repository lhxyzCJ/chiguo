#!/usr/bin/env python3
"""test_topics.py — chiguo_topics 话题选择器单元测试（7 源加权 + 人格调制 + Ebbinghaus）"""

import json
import math
import os
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_topics import TopicPicker
from chiguo_state import ChiguoState
from memory_bridge import MemoryBridge


def _picker_cfg() -> dict:
    with open("chiguo_proactive.toml", "rb") as f:
        return tomllib.load(f).get("topic_picker", {})


def _real_state(tmp: str) -> ChiguoState:
    """真实 toml + 临时目录锚定；mem0 显式禁用（CHIGUO_MEM0_DISABLED=1）→ memory 源静默跳过"""
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    # 时间炸弹锚定：on_break 内部用 datetime.now()（真实时钟）与 semester_end 比较。
    # 固定 semester_end 为过去日期 → on_break 恒 True → schedule_status 走确定性的
    # 假期分支（class_load=free），不依赖真实运行日期。否则学期内（真实今天 ≤ toml
    # 的 semester_end）会走真实课表：14:00 若在课上 → _schedule_topic 返回 None →
    # schedule 源消失，权重分布断言 flaky。
    cfg["schedule"]["semester_end"] = "2025-01-01"
    return ChiguoState(cfg)


class _EmptyAnniversaryMgr:
    """空纪念日源：MockState 默认无纪念日（真实文件场景走 _real_state + base_dir 锚定）"""

    def get_today(self, today):
        return []

    def get_upcoming(self, today, days=7):
        return []


class MockState:
    """最小化 mock：TopicPicker 只依赖 emotion 变化率 / personality / schedule_status / memory_bridge / anniversary_mgr"""

    def __init__(self, bridge=None, personality=None, lon_rate=0.0, anx_rate=0.0,
                 schedule_status=None, quiet_window=(0, 8), anniversary_mgr=None):
        self.memory_bridge = bridge
        self.personality = personality
        self.emotion = SimpleNamespace(loneliness_rate=lon_rate, anxiety_rate=anx_rate)
        self.cooldown = SimpleNamespace(
            trigger_history=[],
            quiet_window=lambda: quiet_window,
            get_trigger_history=lambda: self.cooldown.trigger_history,
        )
        self.anniversary_mgr = anniversary_mgr if anniversary_mgr is not None else _EmptyAnniversaryMgr()
        self._schedule_status = schedule_status or {
            "in_class": False, "class_load": "free", "remaining_classes": 0,
        }

    def schedule_status(self, now):
        return self._schedule_status


class FakeBridge:
    """假 MemoryBridge：available=True，返回固定记忆（不依赖 mem0）"""

    def __init__(self, memories):
        self._memories = memories

    @property
    def available(self):
        return True

    def search_with_forgetting(self, query, limit=3, min_importance=0.3):
        return list(self._memories)

    def random_memory_with_forgetting(self, min_importance=0.5, prefer_categories=None):
        return random.choice(self._memories) if self._memories else None


MEMORY_PREF = {"l0_abstract": "哥哥喜欢喝乌龙茶", "text": "", "memory_category": "preferences",
               "importance": 0.8, "timestamp": 1}
MEMORY_EVENT = {"l0_abstract": "一起去过苏州旅行", "text": "", "memory_category": "events",
                "importance": 0.7, "timestamp": 1}

NETEASE_FIXED = {"type": "netease_music", "hint": "x", "tone": "casual", "data": {}}


class FakeNeteaseService:
    """假网易云策略层：记录 peek/consume 调用；可固定返回话题或抛异常（不限配额）。
    music_topic = peek + consume（与真实 NeteaseService 语义一致）"""

    def __init__(self, topic=None, raise_exc=None, enabled=True):
        self.enabled = enabled  # A3: 与真实 NeteaseService 同门控
        self.peek_calls = []
        self.consume_music_calls = 0
        self.consume_fault_calls = 0
        self._topic = topic
        self._raise_exc = raise_exc

    def peek_music_topic(self, now, in_class=False, in_quiet_window=False):
        self.peek_calls.append({"now": now, "in_class": in_class,
                                "in_quiet_window": in_quiet_window})
        if self._raise_exc:
            raise self._raise_exc
        return self._topic

    def consume_music_topic(self, now):
        self.consume_music_calls += 1

    def consume_fault_topic(self, now):
        self.consume_fault_calls += 1

    def music_topic(self, now, in_class=False, in_quiet_window=False):
        topic = self.peek_music_topic(now, in_class=in_class,
                                      in_quiet_window=in_quiet_window)
        if topic:
            if topic.get("type") == "netease_fault":
                self.consume_fault_topic(now)
            else:
                self.consume_music_topic(now)
        return topic


# ═══════════════════════════════════════════════════════════
# 7 源话题注入：权重分布
# ═══════════════════════════════════════════════════════════

def test_pick_always_returns_valid_topic():
    """真实 state（mem0 禁用）：300 种子 pick 恒返回合法结构（general 永远可用）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg())
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        valid = {"schedule", "weather", "general", "solar_term",
                 "anniversary", "memory", "preference_followup"}
        for i in range(300):
            random.seed(1000 + i)
            t = picker.pick(now)
            assert t and t.get("type") in valid, f"seed {i}: invalid topic {t}"
            assert t.get("hint"), f"seed {i}: empty hint"
            assert t.get("tone") in ("casual", "caring"), f"seed {i}: bad tone"
    print("  OK test_pick_always_returns_valid_topic")


def test_pick_weight_distribution():
    """6/15 14:00 可用源为 schedule/weather/general（solar/anniversary/memory 不可用）→ 加权分布
    权重 0.30/0.20/0.25 → 归一化 ≈ 40%/26.7%/33.3%（2000 种子实测 800/533/667 附近）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg())
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        counts = {}
        for i in range(2000):
            random.seed(2000 + i)
            t = picker.pick(now)
            counts[t["type"]] = counts.get(t["type"], 0) + 1
        assert counts.get("schedule", 0) > 650, f"schedule ~800 expected, got {counts}"
        assert counts.get("general", 0) > 550, f"general ~667 expected, got {counts}"
        assert counts.get("weather", 0) > 430, f"weather ~533 expected, got {counts}"
        assert counts.get("solar_term", 0) == 0, f"no solar term near 06-15, got {counts}"
    print("  OK test_pick_weight_distribution")


def test_high_rate_modulation():
    """情绪快速变化：general×1.5 / weather×0.7 / solar×0.6（实测 1500 种子对比）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg())
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

        def dist(lon_rate):
            state.emotion.loneliness_rate = lon_rate
            c = {}
            for i in range(1500):
                random.seed(3000 + i)
                t = picker.pick(now)
                c[t["type"]] = c.get(t["type"], 0) + 1
            return c

        c_low = dist(0.0)
        c_high = dist(5.0)
        assert c_high["general"] > c_low["general"], \
            f"general should rise: {c_low['general']} → {c_high['general']}"
        assert c_high["weather"] < c_low["weather"], \
            f"weather should drop: {c_low['weather']} → {c_high['weather']}"
    print("  OK test_high_rate_modulation")


def test_personality_modulation_memory_weight():
    """人格调制：高开放性 → memory 话题占比更高（openness_bonus 1.0~2.0，实测 low≈17% / high≈27%）"""
    from chiguo_personality import PersonalityTraits
    with tempfile.TemporaryDirectory() as td:
        bridge = FakeBridge([MEMORY_PREF, MEMORY_EVENT])
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

        def memory_share(openness, n=600):
            pers = PersonalityTraits(openness=openness)
            state = MockState(bridge=bridge, personality=pers)
            picker = TopicPicker(state, _picker_cfg())
            c = {}
            for i in range(n):
                random.seed(4000 + i)
                t = picker.pick(now)
                c[t["type"]] = c.get(t["type"], 0) + 1
            return c.get("memory", 0)

        m_high = memory_share(90.0)
        m_low = memory_share(5.0)
        assert m_high > m_low + 20, f"openness should boost memory: low={m_low} high={m_high}"
    print(f"  OK test_personality_modulation_memory_weight: low={m_low} high={m_high}")


def test_low_openness_boosts_schedule_and_general():
    """低开放性（bonus<1.3）→ schedule×1.3 / general×1.2（实测 1500 种子对比）"""
    from chiguo_personality import PersonalityTraits
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

        def share(openness):
            picker = TopicPicker(state, _picker_cfg())
            state.personality = PersonalityTraits(openness=openness)
            c = {}
            for i in range(1500):
                random.seed(5000 + i)
                t = picker.pick(now)
                c[t["type"]] = c.get(t["type"], 0) + 1
            return c

        c_high = share(90.0)
        c_low = share(5.0)
        assert c_low["schedule"] > c_high["schedule"], \
            f"low openness should boost schedule: {c_high['schedule']} → {c_low['schedule']}"
        assert c_low["general"] > c_high["general"], \
            f"low openness should boost general: {c_high['general']} → {c_low['general']}"
    print("  OK test_low_openness_boosts_schedule_and_general")


# ═══════════════════════════════════════════════════════════
# Ebbinghaus 加权路径（memory_bridge 纯函数 + 排序 + 随机加权）
# ═══════════════════════════════════════════════════════════

def test_ebbinghaus_weight_pure():
    """遗忘权重 R = e^(-t/(S·importance))：新记忆=1.0；半衰期点≈e^-1；min_weight 兜底；时间戳缺失=1.0"""
    bridge = MemoryBridge()  # 不连库，纯函数路径
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    now_ts = now.timestamp()
    fresh = {"timestamp": now_ts, "importance": 0.5}
    assert abs(bridge.ebbinghaus_weight(fresh, now) - 1.0) < 1e-9, "age=0 → 1.0"
    age_s = 168 * 0.5 * 3600  # S(168h) × importance(0.5) → e^-1
    old = {"timestamp": now_ts - age_s, "importance": 0.5}
    w = bridge.ebbinghaus_weight(old, now)
    assert abs(w - math.exp(-1)) < 0.02, f"expected ≈e^-1={math.exp(-1):.4f}, got {w}"
    ancient = {"timestamp": now_ts - 100 * 86400, "importance": 0.5}
    assert abs(bridge.ebbinghaus_weight(ancient, now) - 0.1) < 1e-9, "min_weight floor 0.1"
    assert bridge.ebbinghaus_weight({}, now) == 1.0, "missing timestamp → no decay"
    assert bridge.ebbinghaus_weight({"timestamp": -1}, now) == 1.0, "negative timestamp → no decay"
    imp0 = {"timestamp": now_ts - 3600, "importance": 0}
    assert bridge.ebbinghaus_weight(imp0, now) >= 0.1, "importance clamped to ≥0.1"
    print("  OK test_ebbinghaus_weight_pure")


def test_search_with_forgetting_orders_by_recency():
    """search_with_forgetting：按 importance×遗忘权重降序（新+重要 > 新+不重要 > 旧+重要[min兜底]）"""
    bridge = MemoryBridge()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    ts = now.timestamp()
    canned = [
        {"l0_abstract": "新记忆-重要", "timestamp": ts - 3600, "importance": 0.9},
        {"l0_abstract": "新记忆-不重要", "timestamp": ts - 3600, "importance": 0.1},
        {"l0_abstract": "旧记忆-重要", "timestamp": ts - 2000 * 3600, "importance": 0.9},
    ]
    bridge.search = lambda query, limit=10, category=None, min_importance=0.3: list(canned)
    results = bridge.search_with_forgetting("test", limit=3, now=now)
    order = [r["l0_abstract"] for r in results]
    assert order == ["新记忆-重要", "新记忆-不重要", "旧记忆-重要"], f"got {order}"
    print("  OK test_search_with_forgetting_orders_by_recency")


def test_random_memory_with_forgetting_prefers_fresh():
    """random_memory_with_forgetting：importance²×遗忘权重 → 新记忆显著优先（实测 ~91%>70%）"""
    bridge = MemoryBridge()
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    ts = now.timestamp()
    canned = [
        {"l0_abstract": "新", "timestamp": ts - 3600, "importance": 0.9},
        {"l0_abstract": "旧", "timestamp": ts - 1000 * 3600, "importance": 0.9},
    ]
    bridge.user_relevant = lambda limit=50, min_importance=0.5, prefer_categories=None: list(canned)
    fresh = 0
    for i in range(200):
        random.seed(6000 + i)
        m = bridge.random_memory_with_forgetting(min_importance=0.5, now=now)
        assert m is not None
        if m["l0_abstract"] == "新":
            fresh += 1
    assert fresh >= 140, f"fresh memory should win ~91%, got {fresh}/200"
    print(f"  OK test_random_memory_with_forgetting_prefers_fresh: {fresh}/200 fresh")


# ═══════════════════════════════════════════════════════════
# 各来源单元路径
# ═══════════════════════════════════════════════════════════

def test_weather_season_and_general_topics():
    """季节感知（夏/冬/春/秋）+ 分时通用关心（上午/午饭/晚上）"""
    picker = TopicPicker(MockState(), _picker_cfg())
    summer = picker._weather_season_topic(datetime(2026, 6, 15, 14, 0, tzinfo=CST))
    assert "防暑" in summer["hint"]
    winter = picker._weather_season_topic(datetime(2026, 12, 15, 14, 0, tzinfo=CST))
    assert "保暖" in winter["hint"]
    spring = picker._weather_season_topic(datetime(2026, 4, 15, 14, 0, tzinfo=CST))
    assert "感冒" in spring["hint"]
    autumn = picker._weather_season_topic(datetime(2026, 9, 15, 14, 0, tzinfo=CST))
    assert "添衣" in autumn["hint"]
    g_morning = picker._general_topic(datetime(2026, 6, 15, 9, 0, tzinfo=CST))
    assert "上午" in g_morning["hint"]
    g_noon = picker._general_topic(datetime(2026, 6, 15, 13, 0, tzinfo=CST))
    assert "午饭" in g_noon["hint"]
    g_evening = picker._general_topic(datetime(2026, 6, 15, 20, 0, tzinfo=CST))
    assert "晚上" in g_evening["hint"]
    print("  OK test_weather_season_and_general_topics")


def test_schedule_topic_branches():
    """_schedule_topic 分支：holiday/weekend/makeup/in_class/free/课上完"""
    picker = TopicPicker(MockState(), _picker_cfg())
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

    def topic_for(status):
        picker.state._schedule_status = status
        return picker._schedule_topic(now)

    h = topic_for({"holiday": "国庆", "class_load": "free"})
    assert h and "假期" in h["hint"], h
    w = topic_for({"weekend": True, "class_load": "free"})
    assert w and "周末" in w["hint"], w
    m = topic_for({"makeup_day": True, "class_load": "free"})
    assert m and "调休" in m["hint"], m
    assert topic_for({"in_class": True, "current_course": "高数"}) is None, "in_class → None"
    f = topic_for({"in_class": False, "class_load": "free", "remaining_classes": 0})
    assert f and "没课" in f["hint"], f
    d = topic_for({"in_class": False, "class_load": "busy", "remaining_classes": 0})
    assert d and "上完" in d["hint"], d
    b = topic_for({"in_class": False, "class_load": "busy", "remaining_classes": 2})
    assert b and "累不累" in b["hint"], b
    print("  OK test_schedule_topic_branches")


def test_solar_terms_topic():
    """节气：06-21 夏至（±1 天窗口）→ solar_term 话题；06-15 无节气 → None"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg())
        st = picker._solar_terms_topic(datetime(2026, 6, 21, 14, 0, tzinfo=CST))
        assert st and st["type"] == "solar_term", st
        assert st["data"]["solar_term"]["name"] == "夏至", st
        assert picker._solar_terms_topic(datetime(2026, 6, 15, 14, 0, tzinfo=CST)) is None
    print("  OK test_solar_terms_topic")


def test_anniversary_topic_today_and_upcoming():
    """纪念日：当天命中 + 7 天内倒计时（3c:TopicPicker 经 state.anniversary_mgr,base_dir 锚定;
    文件先于 state 构造写入,替代旧 cwd 重构造手法）"""
    picker_cfg = _picker_cfg()
    with tempfile.TemporaryDirectory() as td:
        Path(td, "anniversaries.json").write_text(json.dumps({
            "anniversaries": [
                {"id": "a1", "type": "anniversary", "name": "相遇纪念日", "date": "06-21"},
                {"id": "a2", "type": "anniversary", "name": "生日", "date": "06-25"},
            ]
        }, ensure_ascii=False))
        state = _real_state(td)
        picker = TopicPicker(state, picker_cfg)
        today = picker._anniversary_topic(datetime(2026, 6, 21, 14, 0, tzinfo=CST))
        assert today and today["type"] == "anniversary", today
        assert "相遇纪念日" in today["hint"], today
        up = picker._anniversary_topic(datetime(2026, 6, 22, 14, 0, tzinfo=CST))
        assert up and up["type"] == "anniversary" and "3天" in up["hint"], up
        assert picker._anniversary_topic(datetime(2026, 7, 10, 14, 0, tzinfo=CST)) is None
    print("  OK test_anniversary_topic_today_and_upcoming")


def test_memory_sources_unavailable_skipped():
    """mem0 不可用 → memory / preference_followup 静默跳过（不崩、返回 None）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg())
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        assert state.memory_bridge.available is False, "precondition: mem0 unavailable"
        assert picker._memory_topic() is None
        assert picker._preference_followup_topic(now) is None
    print("  OK test_memory_sources_unavailable_skipped")


def test_memory_and_preference_topics_with_bridge():
    """bridge 可用：_memory_topic 返回记忆话题（Ebbinghaus 路径），_preference_followup 返回追问话题"""
    with tempfile.TemporaryDirectory() as td:
        bridge = FakeBridge([MEMORY_PREF, MEMORY_EVENT])
        picker = TopicPicker(MockState(bridge=bridge), _picker_cfg())
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        # 显式播种：_preference_followup 内部走 FakeBridge.random_memory_with_forgetting
        # → random.choice 50/50 挑 PREF/EVENT，依赖模块级未播种 RNG 残留会偶发抽中
        # EVENT → "乌龙茶" 断言 flaky。seed(1) 首个 choice 命中 PREF，确定性。
        random.seed(1)
        t = picker._memory_topic()
        assert t and t["type"] == "memory", t
        assert t["data"]["memory"]["l0_abstract"], t
        random.seed(1)
        p = picker._preference_followup_topic(now)
        assert p and p["type"] == "preference_followup", p
        assert "乌龙茶" in p["hint"], p
    print("  OK test_memory_and_preference_topics_with_bridge")


# ═══════════════════════════════════════════════════════════
# v9 来源 8：网易云音乐（策略层委托）
# ═══════════════════════════════════════════════════════════

def test_netease_weight_in_weights():
    """真实 toml 构造后 picker.weights['netease'] == 0.12"""
    picker = TopicPicker(MockState(), _picker_cfg())
    assert picker.weights["netease"] == 0.12, picker.weights
    print("  OK test_netease_weight_in_weights")


def test_netease_service_called_with_gate_args():
    """fake service 记录调用参数：peek_music_topic 收到与 state 一致的 in_class/in_quiet_window"""
    bridge = FakeBridge([MEMORY_PREF, MEMORY_EVENT])
    now_day = datetime(2026, 6, 15, 14, 0, tzinfo=CST)   # 14:00 不在 (0,8) 静默窗
    now_night = datetime(2026, 6, 15, 3, 0, tzinfo=CST)  # 03:00 在 (0,8) 静默窗
    # schedule_status 返回 in_class=True + 非静默 → 传 in_class=True, in_quiet_window=False
    svc = FakeNeteaseService(topic=NETEASE_FIXED)
    state = MockState(bridge=bridge,
                      schedule_status={"in_class": True, "class_load": "busy"})
    picker = TopicPicker(state, _picker_cfg(), netease_service=svc)
    for i in range(20):
        random.seed(8000 + i)
        picker.pick(now_day)
    assert len(svc.peek_calls) == 20, f"每次 pick 都应 peek, got {len(svc.peek_calls)}"
    assert all(c["in_class"] is True and c["in_quiet_window"] is False
               for c in svc.peek_calls), svc.peek_calls
    # schedule_status 返回 in_class=False + 静默窗内 → 传 in_class=False, in_quiet_window=True
    svc2 = FakeNeteaseService(topic=NETEASE_FIXED)
    state2 = MockState(bridge=bridge,
                       schedule_status={"in_class": False, "class_load": "free"})
    picker2 = TopicPicker(state2, _picker_cfg(), netease_service=svc2)
    for i in range(20):
        random.seed(8100 + i)
        picker2.pick(now_night)
    assert len(svc2.peek_calls) == 20, f"每次 pick 都应 peek, got {len(svc2.peek_calls)}"
    assert all(c["in_class"] is False and c["in_quiet_window"] is True
               for c in svc2.peek_calls), svc2.peek_calls
    print("  OK test_netease_service_called_with_gate_args")


def test_netease_disabled_no_peek_no_topic():
    """A3: enabled=False → _netease_music_topic 短路不探测(fake 无 peek 记录)、
    pick 不产出 netease 话题(配置门控语义)"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        svc = FakeNeteaseService(topic=NETEASE_FIXED, enabled=False)
        picker = TopicPicker(state, _picker_cfg(), netease_service=svc)
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        for i in range(50):
            random.seed(9000 + i)
            t = picker.pick(now)
            assert t is not None and t.get("type") != "netease_music", t
        assert len(svc.peek_calls) == 0, "enabled=False 不应探测(无 peek 调用)"
        assert svc.consume_music_calls == 0 and svc.consume_fault_calls == 0
    print("  OK test_netease_disabled_no_peek_no_topic")


def test_netease_consume_only_when_selected():
    """两阶段：peek 每次 pick 都探测（不消费）；consume 只在 netease 被抽中时调用
    （consume 次数 == 选中次数 < 候选生成次数）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        svc = FakeNeteaseService(topic=NETEASE_FIXED)
        picker = TopicPicker(state, _picker_cfg(), netease_service=svc)
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        selected = 0
        n = 300
        for i in range(n):
            random.seed(7000 + i)
            t = picker.pick(now)
            if t.get("type") == "netease_music":
                selected += 1
        assert len(svc.peek_calls) == n, f"每次 pick 都应 peek, got {len(svc.peek_calls)}"
        assert svc.consume_music_calls == selected, \
            f"consume 应等于选中次数: consume={svc.consume_music_calls} selected={selected}"
        assert svc.consume_fault_calls == 0, "fake 恒产 netease_music,不应消费 fault 配额"
        assert 0 < selected < n, f"需偶发选中(非 0 非全选), got {selected}"
        assert svc.consume_music_calls < len(svc.peek_calls), \
            "未选中时不得消费(consume < peek)"
    print(f"  OK test_netease_consume_only_when_selected: "
          f"peek={n} selected={selected} consume={svc.consume_music_calls}")


def test_netease_fault_consume_when_selected():
    """netease_fault 被选中 → consume_fault_topic（选中才消费）"""
    fault = {"type": "netease_fault", "hint": "x", "tone": "playful", "data": {}}
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        svc = FakeNeteaseService(topic=fault)
        cfg = dict(_picker_cfg())
        cfg["netease_weight"] = 1000.0  # 高权重 → 几乎必然选中 netease
        picker = TopicPicker(state, cfg, netease_service=svc)
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        selected = 0
        n = 100
        for i in range(n):
            random.seed(9500 + i)
            t = picker.pick(now)
            if t.get("type") == "netease_fault":
                selected += 1
        assert len(svc.peek_calls) == n
        assert svc.consume_fault_calls == selected, \
            f"fault consume 应等于选中次数: consume={svc.consume_fault_calls} selected={selected}"
        assert selected >= 90, f"高权重下应几乎全选, got {selected}"
        assert svc.consume_music_calls == 0
    print(f"  OK test_netease_fault_consume_when_selected: selected={selected}")


def test_netease_gate_exception_fail_closed():
    """门控信息异常（schedule_status/quiet_window 抛异常）→ fail-closed：
    不发音乐话题、不调 service（防止上课/睡眠时误发）"""
    svc = FakeNeteaseService(topic=NETEASE_FIXED)
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

    def boom():
        raise RuntimeError("boom")

    state1 = MockState()
    state1.schedule_status = lambda now: boom()
    picker1 = TopicPicker(state1, _picker_cfg(), netease_service=svc)
    assert picker1._netease_music_topic(now) is None
    assert svc.peek_calls == [], "fail-closed:门控异常时不得调用 service"

    state2 = MockState()
    state2.cooldown.quiet_window = boom
    picker2 = TopicPicker(state2, _picker_cfg(), netease_service=svc)
    assert picker2._netease_music_topic(now) is None
    assert svc.peek_calls == [], "fail-closed:门控异常时不得调用 service"
    print("  OK test_netease_gate_exception_fail_closed")


def test_netease_not_injected_no_crash():
    """不注入 netease_service → pick 不抛异常，结果类型不含 netease_music/netease_fault"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg())
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        for i in range(300):
            random.seed(9000 + i)
            t = picker.pick(now)
            assert t.get("type") not in ("netease_music", "netease_fault"), \
                f"seed {i}: {t}"
    print("  OK test_netease_not_injected_no_crash")


def test_netease_service_exception_silent():
    """fake service 的 peek_music_topic 抛 RuntimeError → pick 不崩（静默跳过）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        svc = FakeNeteaseService(raise_exc=RuntimeError("boom"))
        picker = TopicPicker(state, _picker_cfg(), netease_service=svc)
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        for i in range(100):
            random.seed(9100 + i)
            t = picker.pick(now)
            assert t and t.get("type"), f"seed {i}: {t}"
    print("  OK test_netease_service_exception_silent")


def test_pick_valid_set_includes_netease():
    """注入恒返回 netease 话题的 fake service + 真实 state：结果类型落在含
    netease_music 的合法集合内（本测试独立构造 valid 集合；fake 恒产 netease_music
    不产 netease_fault，故集合不含 fault 类型）"""
    with tempfile.TemporaryDirectory() as td:
        state = _real_state(td)
        picker = TopicPicker(state, _picker_cfg(),
                             netease_service=FakeNeteaseService(topic=NETEASE_FIXED))
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        valid = {"schedule", "weather", "general", "solar_term",
                 "anniversary", "memory", "preference_followup",
                 "netease_music"}
        seen = set()
        for i in range(300):
            random.seed(9200 + i)
            t = picker.pick(now)
            assert t and t.get("type") in valid, f"seed {i}: invalid topic {t}"
            assert t.get("hint"), f"seed {i}: empty hint"
            assert t.get("tone") in ("casual", "caring"), f"seed {i}: bad tone {t}"
            seen.add(t["type"])
        assert "netease_music" in seen, f"300 种子应出现 netease_music, got {seen}"
    print("  OK test_pick_valid_set_includes_netease")


# ═══════════════════════════════════════════════════════════
# A9: 内容级防复读（3-gram Jaccard 查重）
# ═══════════════════════════════════════════════════════════

def _dedup_picker(recent_sent_texts, **kwargs):
    """可用候选收窄为 weather + general（schedule in_class → None；mem0/anniv/solar 不可用），
    netease 不注入 → 查重行为确定可断言的场景。"""
    state = MockState(schedule_status={"in_class": True, "current_course": "高数"},
                      bridge=FakeBridge([]))
    return TopicPicker(state, _picker_cfg(), recent_sent_texts=recent_sent_texts, **kwargs)


def test_repeat_dedup_rejects_high_similarity():
    """高相似候选（jaccard ≥ 0.6）被弃用：general hint 与最近已发消息相同 → 只剩 weather"""
    now = datetime(2026, 6, 15, 9, 0, tzinfo=CST)  # 上午 → general hint 固定
    general_hint = "问问哥哥今天上午有什么安排"
    picker = _dedup_picker([general_hint])
    assert picker.repeat_jaccard_threshold == 0.6
    assert picker.recent_sent_texts == [general_hint]
    for i in range(50):
        random.seed(20000 + i)
        t = picker.pick(now)
        assert t is not None, f"seed {i}: weather 仍可用"
        assert t["type"] == "weather", f"seed {i}: general 应被弃用, got {t}"
    print("  OK test_repeat_dedup_rejects_high_similarity")


def test_repeat_dedup_low_similarity_normal():
    """低相似最近消息 → 不弃用任何候选（general/weather 正常竞争）"""
    now = datetime(2026, 6, 15, 9, 0, tzinfo=CST)
    picker = _dedup_picker(["最近在追什么番剧"])
    seen = set()
    for i in range(200):
        random.seed(20100 + i)
        t = picker.pick(now)
        assert t is not None and t["type"] in ("general", "weather"), f"seed {i}: {t}"
        seen.add(t["type"])
    assert seen == {"general", "weather"}, f"两种候选都应出现, got {seen}"
    print("  OK test_repeat_dedup_low_similarity_normal")


def test_repeat_dedup_all_rejected_returns_none():
    """全部候选被弃用 → topic 空注入（返回 None）"""
    now = datetime(2026, 6, 15, 9, 0, tzinfo=CST)
    weather_hint = "天气很热，提醒哥哥注意防暑、多喝水"
    general_hint = "问问哥哥今天上午有什么安排"
    picker = _dedup_picker([general_hint, weather_hint])
    for i in range(30):
        random.seed(20200 + i)
        assert picker.pick(now) is None, f"seed {i}: 全部候选应被弃用"
    print("  OK test_repeat_dedup_all_rejected_returns_none")


def test_repeat_dedup_threshold_configurable():
    """阈值 0.0 → 非空候选全部弃用（jaccard ≥ 0 恒真）；阈值 1.0 → 不弃用"""
    now = datetime(2026, 6, 15, 9, 0, tzinfo=CST)
    cfg = dict(_picker_cfg())
    cfg["repeat_jaccard_threshold"] = 0.0
    picker = TopicPicker(MockState(schedule_status={"in_class": True}, bridge=FakeBridge([])),
                         cfg, recent_sent_texts=["随便一条历史消息"])
    for i in range(20):
        random.seed(20300 + i)
        assert picker.pick(now) is None, f"seed {i}: 阈值 0 应全弃用"
    cfg["repeat_jaccard_threshold"] = 1.0
    picker2 = TopicPicker(MockState(schedule_status={"in_class": True}, bridge=FakeBridge([])),
                          cfg, recent_sent_texts=["随便一条历史消息"])
    for i in range(100):
        random.seed(20400 + i)
        assert picker2.pick(now) is not None, f"seed {i}: 阈值 1 不应弃用"
    print("  OK test_repeat_dedup_threshold_configurable")


def test_repeat_dedup_history_n_truncated():
    """repeat_history_n=5：注入 8 条 → 只保留最近 5 条"""
    picker = _dedup_picker([f"历史消息{i}" for i in range(8)])
    assert len(picker.recent_sent_texts) == 5, picker.recent_sent_texts
    assert picker.recent_sent_texts == [f"历史消息{i}" for i in range(5)]
    print("  OK test_repeat_dedup_history_n_truncated")


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("test_topics.py\n")
    tests = [
        test_pick_always_returns_valid_topic,
        test_pick_weight_distribution,
        test_high_rate_modulation,
        test_personality_modulation_memory_weight,
        test_low_openness_boosts_schedule_and_general,
        test_ebbinghaus_weight_pure,
        test_search_with_forgetting_orders_by_recency,
        test_random_memory_with_forgetting_prefers_fresh,
        test_weather_season_and_general_topics,
        test_schedule_topic_branches,
        test_solar_terms_topic,
        test_anniversary_topic_today_and_upcoming,
        test_memory_sources_unavailable_skipped,
        test_memory_and_preference_topics_with_bridge,
        test_netease_weight_in_weights,
        test_netease_service_called_with_gate_args,
        test_netease_disabled_no_peek_no_topic,
        test_netease_consume_only_when_selected,
        test_netease_fault_consume_when_selected,
        test_netease_gate_exception_fail_closed,
        test_netease_not_injected_no_crash,
        test_netease_service_exception_silent,
        test_pick_valid_set_includes_netease,
        # A9 内容级防复读
        test_repeat_dedup_rejects_high_similarity,
        test_repeat_dedup_low_similarity_normal,
        test_repeat_dedup_all_rejected_returns_none,
        test_repeat_dedup_threshold_configurable,
        test_repeat_dedup_history_n_truncated,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    total = len(tests)
    passed = total - failed
    print(f"ALL {total} tests, {passed} passed, {failed} failed.")
    if failed:
        sys.exit(1)
    sys.exit(0)
