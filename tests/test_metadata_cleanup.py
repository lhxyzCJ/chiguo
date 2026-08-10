#!/usr/bin/env python3
"""test_metadata_cleanup.py — C3 死 metadata 清理测试（text 兜底优先）

覆盖: chiguo_topics 的 _memory_topic/_preference_followup_topic 读 path、
chiguo_trigger 的 follow_up 记忆兜底读 path、memory_bridge 展示用读 path——
一律 text 优先，l0_abstract/memory_category 仅作空值兜底（不依赖死字段）。
零 LLM、零网络。
"""

import os
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_topics import TopicPicker  # noqa: E402
from chiguo_trigger import evaluate_triggers  # noqa: E402
from chiguo_state import ChiguoState  # noqa: E402
from memory_bridge import fmt_search_row  # noqa: E402


def _picker_cfg() -> dict:
    with open("chiguo_proactive.toml", "rb") as f:
        return tomllib.load(f).get("topic_picker", {})


class _EmptyAnniversaryMgr:
    def get_today(self, today):
        return []

    def get_upcoming(self, today, days=7):
        return []


class MockState:
    """最小化 mock：TopicPicker 依赖 emotion/personality/schedule/memory_bridge/anniv"""

    def __init__(self, bridge, personality=None):
        from chiguo_personality import PersonalityTraits
        self.memory_bridge = bridge
        self.personality = personality if personality is not None else PersonalityTraits()
        self.emotion = mock.Mock(loneliness_rate=0.0, anxiety_rate=0.0)
        self.cooldown = mock.Mock(trigger_history=[])
        self.anniversary_mgr = _EmptyAnniversaryMgr()
        self.config = {"memory": {}}
        self._schedule_status = {"in_class": False, "class_load": "free",
                                 "remaining_classes": 0}

    def schedule_status(self, now):
        return self._schedule_status


class FakeBridge:
    """可用内存桥：固定返回记忆（text/l0_abstract 可控）。"""

    available = True

    def __init__(self, memories):
        self._memories = memories

    def search_with_forgetting(self, query, limit=3, min_importance=0.3, **kw):
        return list(self._memories)

    def random_memory_with_forgetting(self, min_importance=0.5,
                                      prefer_categories=None, **kw):
        return self._memories[0] if self._memories else None

    def user_relevant(self, limit=20, min_importance=0.3,
                      prefer_categories=None):
        return list(self._memories[:limit])


NOW = datetime(2026, 6, 15, 14, 0, tzinfo=CST)


# ── chiguo_topics._memory_topic：text 优先 ────────────────

def test_memory_topic_text_primary():
    """记忆只有 text（无 l0_abstract）→ hint 用 text 生成（死字段不再挡路）。"""
    mem = {"id": "m1", "text": "哥哥喜欢喝美式咖啡，不加糖",
           "l0_abstract": "", "category": "preferences",
           "importance": 0.8, "timestamp": 1}
    picker = TopicPicker(MockState(FakeBridge([mem])), _picker_cfg())
    t = picker._memory_topic()
    assert t and t["type"] == "memory", t
    assert "美式咖啡" in t["hint"], t["hint"]
    assert t["data"]["memory"]["id"] == "m1"
    print("  OK test_memory_topic_text_primary")


def test_memory_topic_falls_back_to_abstract():
    """text 为空 → 回退 l0_abstract（不产生空 hint）。"""
    mem = {"id": "m1", "text": "", "l0_abstract": "一起去过苏州旅行",
           "memory_category": "events", "importance": 0.7, "timestamp": 1}
    picker = TopicPicker(MockState(FakeBridge([mem])), _picker_cfg())
    t = picker._memory_topic()
    assert t and "苏州" in t["hint"], t
    print("  OK test_memory_topic_falls_back_to_abstract")


def test_memory_topic_category_priority():
    """category 字段优先于 memory_category（preferences → tone=casual）。"""
    mem = {"id": "m1", "text": "喜欢喝咖啡", "category": "preferences",
           "memory_category": "?",
           "importance": 0.8, "timestamp": 1}
    picker = TopicPicker(MockState(FakeBridge([mem])), _picker_cfg())
    t = picker._memory_topic()
    assert t and t["tone"] == "casual", t
    # 无 category 且 memory_category=preferences → 也走 casual（旧字段兜底）
    mem2 = {"id": "m2", "text": "喜欢喝咖啡", "category": "",
            "memory_category": "preferences", "importance": 0.8, "timestamp": 1}
    picker2 = TopicPicker(MockState(FakeBridge([mem2])), _picker_cfg())
    t2 = picker2._memory_topic()
    assert t2 and t2["tone"] == "casual", t2
    print("  OK test_memory_topic_category_priority")


def test_memory_topic_empty_text_no_hint():
    """text 与 l0_abstract 皆空 → 返回 None（不产出空 hint 话题）。"""
    mem = {"id": "m1", "text": "", "l0_abstract": "",
           "importance": 0.8, "timestamp": 1}
    picker = TopicPicker(MockState(FakeBridge([mem])), _picker_cfg())
    assert picker._memory_topic() is None
    print("  OK test_memory_topic_empty_text_no_hint")


# ── chiguo_topics._preference_followup_topic：text 优先 ──

def test_preference_followup_text_primary():
    """偏好追问：记忆只有 text → hint 用 text。"""
    mem = {"id": "m1", "text": "哥哥上次说周末去爬山",
           "l0_abstract": "", "category": "preferences",
           "importance": 0.8, "timestamp": 1}
    picker = TopicPicker(MockState(FakeBridge([mem])), _picker_cfg())
    t = picker._preference_followup_topic(NOW)
    assert t and t["type"] == "preference_followup", t
    assert "爬山" in t["hint"], t["hint"]
    print("  OK test_preference_followup_text_primary")


def test_preference_followup_falls_back_to_abstract():
    """text 为空 → 回退 l0_abstract。"""
    mem = {"id": "m1", "text": "", "l0_abstract": "哥哥想换新手机",
           "category": "preferences", "importance": 0.8, "timestamp": 1}
    picker = TopicPicker(MockState(FakeBridge([mem])), _picker_cfg())
    t = picker._preference_followup_topic(NOW)
    assert t and "手机" in t["hint"], t
    print("  OK test_preference_followup_falls_back_to_abstract")


# ── chiguo_trigger follow_up 记忆兜底：text 优先 ─────────

def _followup_state(tmp: str, mem: dict) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    s = ChiguoState(cfg)
    s.emotion.energy = 40  # 排除 playful 干扰
    s.cooldown.last_user_message_at = (NOW - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = NOW.strftime("%Y-%m-%d")
    s.memory_bridge = FakeBridge([mem])
    return s


def test_trigger_followup_memory_text_primary():
    """follow_up 记忆兜底：记忆只有 text → topic 用 text。"""
    with tempfile.TemporaryDirectory() as td:
        mem = {"timestamp": (NOW - timedelta(hours=3)).timestamp(),
               "text": "哥哥说想养一只猫", "l0_abstract": "",
               "importance": 0.8}
        s = _followup_state(td, mem)
        with mock.patch("chiguo_trigger.random.random", return_value=0.4):
            t = evaluate_triggers(s, NOW)
        assert t is not None and t.type == "follow_up", t
        assert t.data["source"] == "memory"
        assert t.data["topic"] == "哥哥说想养一只猫", t.data
    print("  OK test_trigger_followup_memory_text_primary")


def test_trigger_followup_memory_falls_back_to_abstract():
    """text 为空 → 回退 l0_abstract。"""
    with tempfile.TemporaryDirectory() as td:
        mem = {"timestamp": (NOW - timedelta(hours=3)).timestamp(),
               "text": "", "l0_abstract": "哥哥说周末要去爬山",
               "importance": 0.8}
        s = _followup_state(td, mem)
        with mock.patch("chiguo_trigger.random.random", return_value=0.4):
            t = evaluate_triggers(s, NOW)
        assert t is not None and t.type == "follow_up", t
        assert t.data["topic"] == "哥哥说周末要去爬山", t.data
    print("  OK test_trigger_followup_memory_falls_back_to_abstract")


# ── memory_bridge 展示用读：text 优先 ────────────────────

def test_fmt_search_row_text_primary():
    """fmt_search_row：text 优先展示（有 text 不显示 l0_abstract）。"""
    row = {"category": "preferences", "text": "喜欢喝美式咖啡",
           "l0_abstract": "旧摘要"}
    out = fmt_search_row(row)
    assert "[preferences] 喜欢喝美式咖啡" == out, out
    assert "旧摘要" not in out, "有 text 时不应展示 l0_abstract"
    print("  OK test_fmt_search_row_text_primary")


def test_fmt_search_row_falls_back_to_abstract():
    """text 为空 → 回退 l0_abstract；text 长度 > 80 → 截断。"""
    assert fmt_search_row({"category": "e", "text": "",
                           "l0_abstract": "回退摘要"}) == "[e] 回退摘要"
    long_text = "字" * 100
    out = fmt_search_row({"category": "e", "text": long_text})
    assert out == f"[e] {'字' * 80}", f"应截断到 80 字, got {len(out)}"
    print("  OK test_fmt_search_row_falls_back_to_abstract")


if __name__ == "__main__":
    print("test_metadata_cleanup.py\n")
    tests = [
        test_memory_topic_text_primary,
        test_memory_topic_falls_back_to_abstract,
        test_memory_topic_category_priority,
        test_memory_topic_empty_text_no_hint,
        test_preference_followup_text_primary,
        test_preference_followup_falls_back_to_abstract,
        test_trigger_followup_memory_text_primary,
        test_trigger_followup_memory_falls_back_to_abstract,
        test_fmt_search_row_text_primary,
        test_fmt_search_row_falls_back_to_abstract,
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
    print(f"\n{'=' * 40}")
    total = len(tests)
    print(f"ALL {total} metadata-cleanup tests, {total - failed} passed, {failed} failed.")
    sys.exit(1 if failed else 0)
