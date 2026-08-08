#!/usr/bin/env python3
"""test_followup.py — 接话茬(pending_topics + follow_up 触发)测试(v7)"""

import os
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_trigger import evaluate_triggers
from chiguo_state import ChiguoState


def _make_state(tmp: str, now: datetime) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    s = ChiguoState(cfg)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    return s


class FakeBridge:
    """内存版 memory_bridge 替身:user_relevant 返回预设记忆,available 恒 True"""
    available = True

    def __init__(self, memories: list[dict]):
        self._memories = memories

    def user_relevant(self, limit: int = 20, min_importance: float = 0.3,
                      prefer_categories: list[str] = None) -> list[dict]:
        return self._memories[:limit]

    def random_memory(self, category: str = None, min_importance: float = 0.4):
        return None


def test_analysis_topic_appends_and_dedupes():
    """带 topic 的分析 → 追加 pending;同话题再来 → 视为接续,移除旧条目重新计时"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.on_user_message(now, analysis={"topic": "比赛"})
        assert len(s.pending_topics) == 1
        assert s.pending_topics[0]["topic"] == "比赛"
        assert s.pending_topics[0]["source"] == "analysis"
        assert s.pending_topics[0]["attempted"] is False
        # 同话题(继续聊) → 移除旧条目,新条目 created_at 刷新
        s.on_user_message(now, analysis={"topic": "比赛"})
        assert len(s.pending_topics) == 1
        assert s.pending_topics[0]["created_at"] == now.isoformat()
        # 不同话题 → 追加第二条
        s.on_user_message(now, analysis={"topic": "电影"})
        assert len(s.pending_topics) == 2
    print("  OK test_analysis_topic_appends_and_dedupes")


def test_topic_resolved_removes():
    """topic_resolved=true → 移除对应话题(指定 topic 按匹配,未指定移除最旧)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.on_user_message(now, analysis={"topic": "比赛"})
        s.on_user_message(now, analysis={"topic": "电影"})
        s.on_user_message(now, analysis={"topic": "比赛", "topic_resolved": True})
        assert [t["topic"] for t in s.pending_topics] == ["电影"]
        s.on_user_message(now, analysis={"topic_resolved": True})
        assert s.pending_topics == []
    print("  OK test_topic_resolved_removes")


def test_invalid_topic_ignored():
    """topic 非字符串/空 → 忽略不崩;analysis 为 None → 不产生话题"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.on_user_message(now, analysis={"topic": 123})
        s.on_user_message(now, analysis={"topic": "  "})
        s.on_user_message(now, analysis=None)
        assert s.pending_topics == []
    print("  OK test_invalid_topic_ignored")


def test_prune_expired_and_attempted():
    """prune_pending_topics: 过期(>48h)/已尝试 → 移除;上限 20 丢弃最旧"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.add_pending_topic("新鲜的", now - timedelta(hours=3))
        s.add_pending_topic("过期的", now - timedelta(hours=50))
        s.add_pending_topic("已尝试的", now - timedelta(hours=5))
        s.mark_pending_topic_attempted("已尝试的")
        s.pending_topics.append("垃圾条目")  # 非 dict 条目 → prune 不崩
        s.prune_pending_topics(now)
        assert [t["topic"] for t in s.pending_topics] == ["新鲜的"]
        # 上限 20
        for i in range(25):
            s.add_pending_topic(f"t{i}", now)
        assert len(s.pending_topics) == 20
        assert s.pending_topics[0]["topic"] == "t5"  # 最旧 5 条被丢弃
    print("  OK test_prune_expired_and_attempted")


def test_pending_topics_persist():
    """pending_topics 存 chiguo_state.json,save/load 往返不丢失"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.add_pending_topic("比赛", now)
        s.save(_backup=False, _increment_tick=False)
        s2 = _make_state(td, now)
        assert [t["topic"] for t in s2.pending_topics] == ["比赛"]
        assert s2.pending_topics[0]["source"] == "analysis"
        assert s2.pending_topics[0]["attempted"] is False
    print("  OK test_pending_topics_persist")


def _run_seeds(s: ChiguoState, now: datetime, n: int = 300, seed0: int = 5000) -> dict:
    counts: dict[str, int] = {}
    for i in range(n):
        random.seed(seed0 + i)
        t = evaluate_triggers(s, now)
        key = t.type if t else "None"
        counts[key] = counts.get(key, 0) + 1
    return counts


def test_follow_up_fires_in_age_window():
    """2-48h 内的 pending 话题 → follow_up 候选(权重足够 → 恒触发)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40  # 排除 playful 干扰
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        counts = _run_seeds(s, now)
        assert counts.get("follow_up", 0) >= 1, counts
    print("  OK test_follow_up_fires_in_age_window")


def test_follow_up_single_attempt():
    """触发后标记 attempted → 不再重复触发(300 次评估 0 次)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        random.seed(42)
        t = evaluate_triggers(s, now)
        assert t is not None and t.type == "follow_up"
        assert s.pending_topics[0]["attempted"] is True
        counts = _run_seeds(s, now)
        assert counts.get("follow_up", 0) == 0, counts
    print("  OK test_follow_up_single_attempt")


def test_follow_up_outside_age_window():
    """年龄 < 2h(刚聊完)或 > 48h(过期) → 不触发"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        s.add_pending_topic("太新", now - timedelta(minutes=30))
        assert _run_seeds(s, now).get("follow_up", 0) == 0
        s.add_pending_topic("太旧", now - timedelta(hours=50))
        assert _run_seeds(s, now).get("follow_up", 0) == 0
    print("  OK test_follow_up_outside_age_window")


def test_follow_up_data_fields():
    """context 数据: topic/source/age_hours 正确"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        random.seed(42)
        t = evaluate_triggers(s, now)
        assert t.data["topic"] == "比赛"
        assert t.data["source"] == "analysis"
        assert 3.5 <= t.data["age_hours"] <= 4.5
    print("  OK test_follow_up_data_fields")


def test_follow_up_expired_pruned():
    """过期话题在评估时被清理(pending_topics 不再残留)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        s.add_pending_topic("过期的", now - timedelta(hours=49))
        _run_seeds(s, now, n=3)
        assert s.pending_topics == []
    print("  OK test_follow_up_expired_pruned")


def test_follow_up_memory_fallback_fires():
    """无 pending 话题 + 近期用户相关记忆 → follow_up 用记忆兜底(source=memory)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        mem = {"timestamp": (now - timedelta(hours=3)).timestamp(),
               "l0_abstract": "哥哥说周末要去爬山", "text": "哥哥说周末要去爬山"}
        s.memory_bridge = FakeBridge([mem])
        random.seed(42)
        t = evaluate_triggers(s, now)
        assert t is not None and t.type == "follow_up", t
        assert t.data["source"] == "memory"
        assert t.data["topic"] == "哥哥说周末要去爬山"
        assert 2.5 <= t.data["age_hours"] <= 3.5
    print("  OK test_follow_up_memory_fallback_fires")


def test_follow_up_memory_fallback_ignores_old_memory():
    """兜底记忆超过 48h → 不触发"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        mem = {"timestamp": (now - timedelta(hours=60)).timestamp(),
               "l0_abstract": "很旧的话题", "text": "很旧的话题"}
        s.memory_bridge = FakeBridge([mem])
        counts = _run_seeds(s, now, n=50)
        assert counts.get("follow_up", 0) == 0, counts
    print("  OK test_follow_up_memory_fallback_ignores_old_memory")


def test_follow_up_multiple_topics_younger_wins():
    """多话题:最老话题权重 < 门槛时不阻塞更年轻话题(修复饿死)"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        s.add_pending_topic("太旧11h", now - timedelta(hours=11))
        s.add_pending_topic("新3h", now - timedelta(hours=3))
        counts = _run_seeds(s, now, n=300)
        assert counts.get("follow_up", 0) >= 1, counts
        # seed 循环中较年轻话题已被触发并标记 attempted → 重新注入后做确定性断言
        s.pending_topics.clear()
        s.add_pending_topic("太旧11h", now - timedelta(hours=11))
        s.add_pending_topic("新3h", now - timedelta(hours=3))
        random.seed(42)
        t = evaluate_triggers(s, now)
        assert t is not None and t.type == "follow_up"
        assert t.data["topic"] == "新3h"  # 3h bell=0.89×0.35 压过 11h
    print("  OK test_follow_up_multiple_topics_younger_wins")


def test_daemon_context_contains_follow_up_hint():
    """daemon 决策:follow_up 触发 → context 带 follow_up 字段 + 指令含接话茬"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 16, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.energy = 40
        s.add_pending_topic("比赛", now - timedelta(hours=4))
        s.save(_backup=False, _increment_tick=False)
        import chiguo_daemon
        engine = chiguo_daemon.DecisionEngine(str(Path(td) / "chiguo_proactive.toml"))
        engine.state.add_pending_topic("比赛", now - timedelta(hours=4))
        engine.state.emotion.energy = 40
        random.seed(42)
        trigger = evaluate_triggers(engine.state, now)
        # 16:00 无仪式候选 + energy=40 排除 playful + 4h 话题 → follow_up 为唯一候选
        assert trigger is not None and trigger.type == "follow_up", trigger
        context = engine._build_context(trigger, now)
        assert context["follow_up"]["topic"] == "比赛"
        assert "接话茬" in context["instruction"]
        assert "比赛" in context["instruction"]
    print("  OK test_daemon_context_contains_follow_up_hint")


if __name__ == "__main__":
    test_analysis_topic_appends_and_dedupes()
    test_topic_resolved_removes()
    test_invalid_topic_ignored()
    test_prune_expired_and_attempted()
    test_pending_topics_persist()
    test_follow_up_fires_in_age_window()
    test_follow_up_single_attempt()
    test_follow_up_outside_age_window()
    test_follow_up_data_fields()
    test_follow_up_expired_pruned()
    test_follow_up_memory_fallback_fires()
    test_follow_up_memory_fallback_ignores_old_memory()
    test_follow_up_multiple_topics_younger_wins()
    test_daemon_context_contains_follow_up_hint()
    print("test_followup.py: ALL PASS")
