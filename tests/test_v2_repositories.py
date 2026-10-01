"""tests/test_v2_repositories.py — Chiguo v2 仓储层（Phase 2）TDD。

覆盖：各表 add→get 往返（dataclass 相等 + frozen）、JSON 列 dict ⇄ TEXT、
时间列 CST 往返、过滤与排序（list_open / due_open / active / recent / for_turn）、
状态迁移（resolve / set_status / mark_fired / touch / close / supersede）、
memories.supersede 的链接 + 状态 + 事务原子性、未知 id 与空库安全、FK 自然抛错。
"""
import dataclasses
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chiguo_time import CST  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402
from storage.repositories.actions import (  # noqa: E402
    Action, ActionRepo, Delivery, DeliveryRepo)
from storage.repositories.commitments import Commitment, CommitmentRepo  # noqa: E402
from storage.repositories.drives import Drive, DriveRepo, Intent, IntentRepo  # noqa: E402
from storage.repositories.memories import Memory, MemoryRepo  # noqa: E402
from storage.repositories.messages import (  # noqa: E402
    Message, MessageRepo, Session, SessionRepo)
from storage.repositories.observations import ObservationRepo, WorldObservation  # noqa: E402
from storage.repositories.opportunities import Opportunity, OpportunityRepo  # noqa: E402
from storage.repositories.schedules import Schedule, ScheduleRepo  # noqa: E402
from storage.repositories.threads import Thread, ThreadRepo  # noqa: E402

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=CST)
CST_OFFSET = timedelta(hours=8)


@pytest.fixture()
def db(tmp_path):
    d = Database(tmp_path / "chiguo.sqlite")
    migrate(d)
    return d


# ── sessions / messages ─────────────────────────────────────────

def test_sessions_and_messages_roundtrip(db):
    sessions = SessionRepo(db)
    messages = MessageRepo(db)
    s = sessions.add("conversation", pi_session_id="pi-123")
    assert isinstance(s, Session)
    assert sessions.get(s.id) == s
    assert s.kind == "conversation" and s.ended_at is None
    assert s.started_at.utcoffset() == CST_OFFSET

    m = messages.add("in", "你好", session_id=s.id, at=T0,
                     analysis={"emotion": "开心", "score": 0.9})
    got = messages.get(m.id)
    assert got == m
    assert isinstance(got, Message)
    assert got.analysis == {"emotion": "开心", "score": 0.9}
    assert got.at == T0
    assert got.at.utcoffset() == CST_OFFSET
    assert got.session_id == s.id
    assert got.event_id is None and got.delivery_id is None


def test_messages_recent_and_for_session(db):
    sessions = SessionRepo(db)
    messages = MessageRepo(db)
    s1 = sessions.add("conversation")
    s2 = sessions.add("conversation")
    messages.add("in", "a", session_id=s1.id, at=T0)
    messages.add("out", "b", session_id=s1.id, at=T0 + timedelta(minutes=1))
    messages.add("in", "c", session_id=s2.id, at=T0 + timedelta(minutes=2))
    assert [m.text for m in messages.recent()] == ["c", "b", "a"]
    assert [m.text for m in messages.recent(limit=2)] == ["c", "b"]
    assert [m.text for m in messages.for_session(s1.id)] == ["a", "b"]
    assert messages.for_session("nope") == []
    assert messages.get("nope") is None


def test_session_end_and_message_fk(db):
    sessions = SessionRepo(db)
    messages = MessageRepo(db)
    s = sessions.add("system")
    end_at = T0 + timedelta(hours=1)
    assert sessions.end(s.id, end_at) is True
    assert sessions.get(s.id).ended_at == end_at
    assert sessions.end("nope", end_at) is False
    with pytest.raises(sqlite3.IntegrityError):
        messages.add("in", "x", session_id="missing")


# ── commitments ─────────────────────────────────────────────────

def test_commitments_roundtrip_and_filters(db):
    repo = CommitmentRepo(db)
    overdue = repo.add("task", "交作业", due_at=T0 - timedelta(hours=1), details="数学")
    future = repo.add("promise", "回电话", due_at=T0 + timedelta(hours=3))
    no_due = repo.add("idea", "随缘")
    got = repo.get(overdue.id)
    assert got == overdue
    assert isinstance(got, Commitment)
    assert got.details == "数学"
    assert got.status == "open" and got.resolved_at is None
    assert got.created_from_event is None
    assert got.created_at.utcoffset() == CST_OFFSET
    # open 列表：有 due 的按 due 升序在前，无 due 的在后
    assert [c.id for c in repo.list_open()] == [overdue.id, future.id, no_due.id]
    assert [c.id for c in repo.due_open(T0)] == [overdue.id]
    assert repo.due_open(T0 - timedelta(hours=2)) == []
    assert repo.get("nope") is None


def test_commitment_resolve_transition(db):
    repo = CommitmentRepo(db)
    c = repo.add("task", "x", due_at=T0)
    when = T0 + timedelta(hours=2)
    assert repo.resolve(c.id, when) is True
    got = repo.get(c.id)
    assert got.status == "done" and got.resolved_at == when
    assert repo.list_open() == []
    assert repo.due_open(when) == []
    # 二次 resolve 幂等无效，不覆盖 resolved_at
    assert repo.resolve(c.id, when + timedelta(hours=1)) is False
    assert repo.get(c.id).resolved_at == when
    assert repo.resolve("nope", when) is False


def test_commitment_created_from_event(db):
    from storage.events import EventStore
    ev = EventStore(db).append("message.received", source="wechat")
    repo = CommitmentRepo(db)
    c = repo.add("task", "复习", created_from_event=ev.event_id)
    assert repo.get(c.id).created_from_event == ev.event_id


# ── threads ─────────────────────────────────────────────────────

def test_threads_lifecycle_and_ordering(db):
    repo = ThreadRepo(db)
    a = repo.open_thread("考试", source="schedule")
    b = repo.open_thread("音乐")
    assert isinstance(a, Thread)
    assert repo.get(a.id) == a
    assert a.state == "open" and a.payload is None and a.source == "schedule"
    assert [t.id for t in repo.list_open()] == [b.id, a.id]  # 最近活跃在前

    touch_at = datetime.now(CST) + timedelta(hours=5)
    assert repo.touch(a.id, touch_at) is True
    assert repo.get(a.id).last_interaction_at == touch_at
    assert [t.id for t in repo.list_open()] == [a.id, b.id]

    close_at = datetime.now(CST) + timedelta(hours=6)
    assert repo.close(b.id, close_at) is True
    closed = repo.get(b.id)
    assert closed.state == "closed" and closed.closed_at == close_at
    assert [t.id for t in repo.list_open()] == [a.id]
    assert repo.touch(b.id, close_at) is False    # 已关闭不可 touch
    assert repo.close(b.id, close_at) is False    # 重复关闭无效果
    assert repo.touch("nope", close_at) is False
    assert repo.close("nope", close_at) is False
    assert repo.get("nope") is None
    assert repo.list_open()[0].payload is None


# ── opportunities ───────────────────────────────────────────────

def test_opportunities_roundtrip_and_expiry(db):
    repo = OpportunityRepo(db)
    o = repo.add("exam_finished", novelty=0.8, relevance=0.9, urgency=0.5,
                 emotional_affordance=0.7, expires_at=T0 + timedelta(hours=2),
                 payload={"course": "数学", "score": 95})
    got = repo.get(o.id)
    assert got == o
    assert isinstance(got, Opportunity)
    assert got.payload == {"course": "数学", "score": 95}
    assert got.expires_at == T0 + timedelta(hours=2)
    assert got.expires_at.utcoffset() == CST_OFFSET
    assert got.status == "open" and got.observation_event_id is None
    forever = repo.add("long_silence")
    assert [x.id for x in repo.list_open(T0)] == [o.id, forever.id]
    # 过期即不在 open（expires_at <= now）
    assert [x.id for x in repo.list_open(T0 + timedelta(hours=3))] == [forever.id]

    assert repo.set_status(o.id, "consumed") is True
    assert repo.get(o.id).status == "consumed"
    assert [x.id for x in repo.list_open(T0)] == [forever.id]
    assert repo.set_status("nope", "consumed") is False
    assert repo.get("nope") is None


# ── drives / intents ────────────────────────────────────────────

def test_drives_for_turn_and_roundtrip(db):
    repo = DriveRepo(db)
    d1 = repo.add("curiosity", 0.7, inputs={"affect": {"valence": 0.2}},
                  autonomous_turn_id="turn-1")
    d2 = repo.add("care", 0.4, autonomous_turn_id="turn-2")
    d3 = repo.add("reflection", 0.3, autonomous_turn_id="turn-1")
    assert isinstance(d1, Drive)
    assert d1.inputs == {"affect": {"valence": 0.2}}
    assert d1.status == "active" and d1.evaluated_at.utcoffset() == CST_OFFSET
    assert d1.autonomous_turn_id == "turn-1"
    assert repo.for_turn("turn-1") == [d1, d3]
    assert repo.for_turn("turn-2") == [d2]
    assert repo.for_turn("nope") == []


def test_intents_roundtrip_and_status(db):
    repo = IntentRepo(db)
    why = {"opportunity_ids": ["o1"], "drive_ids": ["d1"], "explanation": "考试结束"}
    it = repo.add("celebrate", why, plan={"tone": "轻快"}, autonomous_turn_id="turn-1")
    got = repo.get(it.id)
    assert got == it
    assert isinstance(got, Intent)
    assert got.why == why and got.plan == {"tone": "轻快"}
    assert got.status == "open" and got.autonomous_turn_id == "turn-1"
    assert repo.set_status(it.id, "done") is True
    assert repo.get(it.id).status == "done"
    assert repo.set_status("nope", "done") is False
    assert repo.get("nope") is None


# ── actions / deliveries ────────────────────────────────────────

def test_actions_roundtrip_and_status_transitions(db):
    intents = IntentRepo(db)
    repo = ActionRepo(db)
    it = intents.add("remind", {"explanation": "到期"})
    a = repo.add("send_message", intent_id=it.id, correlation_id="conv-1",
                 input={"text": "在吗"})
    got = repo.get(a.id)
    assert got == a
    assert isinstance(got, Action)
    assert a.status == "pending" and a.started_at is None and a.completed_at is None
    assert a.correlation_id == "conv-1" and a.input == {"text": "在吗"}
    assert a.cause_event_id is None
    assert repo.for_intent(it.id) == [a]
    assert repo.for_intent("nope") == []

    assert repo.set_status(a.id, "started") is True
    mid = repo.get(a.id)
    assert mid.status == "started" and mid.started_at is not None
    assert mid.started_at.utcoffset() == CST_OFFSET and mid.completed_at is None

    assert repo.set_status(a.id, "completed", output={"message_id": "m1"}) is True
    done = repo.get(a.id)
    assert done.status == "completed" and done.completed_at is not None
    assert done.output == {"message_id": "m1"} and done.error is None
    assert repo.set_status("nope", "completed") is False
    assert repo.get("nope") is None


def test_action_failure_records_error(db):
    repo = ActionRepo(db)
    a = repo.add("send_message")
    assert a.input is None
    assert repo.set_status(a.id, "failed", error="boom") is True
    got = repo.get(a.id)
    assert got.status == "failed" and got.error == "boom"
    assert got.completed_at is not None


def test_deliveries_roundtrip_and_status(db):
    actions = ActionRepo(db)
    repo = DeliveryRepo(db)
    a = actions.add("send_message")
    d = repo.add(a.id, "wechat", "sent")
    got = repo.get(d.id)
    assert got == d
    assert isinstance(got, Delivery)
    assert got.attempts == 1 and got.sent_at is not None
    assert got.sent_at.utcoffset() == CST_OFFSET
    assert got.error is None and got.provider_message_id is None

    failed = repo.add(a.id, "wechat", "failed")
    assert failed.sent_at is None
    assert repo.set_status(failed.id, "sent", provider_message_id="pm-1") is True
    retried = repo.get(failed.id)
    assert retried.status == "sent" and retried.provider_message_id == "pm-1"
    assert retried.sent_at is not None
    assert repo.set_status(failed.id, "failed", error="timeout") is True
    assert repo.set_status(failed.id, "failed", error="timeout") is True
    assert repo.get(failed.id).error == "timeout"
    assert repo.set_status("nope", "sent") is False
    assert repo.get("nope") is None


def test_delivery_fk_requires_action(db):
    repo = DeliveryRepo(db)
    with pytest.raises(sqlite3.IntegrityError):
        repo.add("missing", "wechat", "sent")


# ── world_observations ──────────────────────────────────────────

def test_observations_recent_and_active(db):
    repo = ObservationRepo(db)
    o1 = repo.add("weather", "weather.changed", T0, {"text": "晴"},
                  expires_at=T0 + timedelta(hours=1))
    o2 = repo.add("netease", "music.observed", T0 + timedelta(minutes=30), {"song": "A"})
    o3 = repo.add("weather", "weather.changed", T0 + timedelta(minutes=45), {"text": "雨"},
                  expires_at=T0 + timedelta(hours=2))
    assert isinstance(o1, WorldObservation)
    assert o1.payload == {"text": "晴"}
    assert o1.observed_at == T0 and o1.observed_at.utcoffset() == CST_OFFSET
    assert o1.event_id is None
    assert repo.recent() == [o3, o2, o1]
    assert repo.recent(source="weather") == [o3, o1]
    assert repo.recent(limit=1) == [o3]
    # active：未过期（expires_at > now 或 NULL），新→旧
    assert repo.active(T0 + timedelta(hours=1, minutes=30)) == [o3, o2]
    assert repo.active(T0 + timedelta(hours=1, minutes=30), source="weather") == [o3]
    assert repo.active(T0) == [o3, o2, o1]
    assert repo.recent(source="nope") == []


# ── memories ────────────────────────────────────────────────────

def test_memories_roundtrip_and_filters(db):
    repo = MemoryRepo(db)
    m = repo.add("user_fact", "喜欢喝拿铁", source="wechat", origin="user",
                 confidence=0.9, observed_at=T0, valid_from=T0,
                 importance=0.8, emotion_tag="warm", meta={"turn": 3})
    got = repo.get(m.id)
    assert got == m
    assert isinstance(got, Memory)
    assert got.meta == {"turn": 3}
    assert got.observed_at == T0 and got.observed_at.utcoffset() == CST_OFFSET
    assert got.valid_from == T0 and got.valid_to is None
    assert got.status == "active" and got.created_at == got.updated_at
    assert got.mem0_id is None

    other = repo.add("episodic", "今天去了公园")
    assert repo.list_active() == [m, other]          # created_at 升序
    assert repo.list_active(kind="episodic") == [other]
    assert repo.set_status(other.id, "stale") is True
    assert repo.get(other.id).status == "stale"
    assert repo.list_active() == [m]
    assert repo.set_status("nope", "stale") is False
    assert repo.get("nope") is None


def test_memory_supersede_links_and_status(db):
    repo = MemoryRepo(db)
    old = repo.add("user_fact", "喜欢 A 乐队", valid_from=T0)
    new = repo.add("user_fact", "不再喜欢 A 乐队")
    when = T0 + timedelta(days=1)
    assert repo.supersede(old.id, new.id, when) is True
    old2 = repo.get(old.id)
    assert old2.status == "superseded"
    assert old2.valid_to == when and old2.updated_at == when
    assert repo.get(new.id).status == "active"
    assert [m.id for m in repo.list_active()] == [new.id]
    # 链接方向：新记忆 supersedes 旧记忆（from=新, to=旧）
    row = db.connect().execute(
        "SELECT * FROM memory_links WHERE relation = 'supersedes'").fetchone()
    assert row is not None
    assert (row["from_memory_id"], row["to_memory_id"]) == (new.id, old.id)
    assert datetime.fromisoformat(row["created_at"]) == when
    # 旧 id 不存在 → False，不插入链接
    assert repo.supersede("missing", new.id, when) is False
    assert db.connect().execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 1


def test_memory_supersede_is_atomic(db):
    repo = MemoryRepo(db)
    old = repo.add("user_fact", "旧事实")
    # 新 id 不存在 → memory_links 外键失败 → 事务整体回滚（旧行保持 active）
    with pytest.raises(sqlite3.IntegrityError):
        repo.supersede(old.id, "missing-new", T0)
    got = repo.get(old.id)
    assert got.status == "active" and got.valid_to is None
    assert db.connect().execute("SELECT COUNT(*) FROM memory_links").fetchone()[0] == 0


# ── schedules ───────────────────────────────────────────────────

def test_schedules_once_and_recurring(db):
    repo = ScheduleRepo(db)
    once = repo.add("once", due_at=T0, payload={"reason": "wake"})
    assert isinstance(once, Schedule)
    assert repo.get(once.id) == once
    assert once.status == "pending" and once.next_fire_at == T0
    assert once.payload == {"reason": "wake"} and once.recurrence is None
    assert repo.due(T0 - timedelta(minutes=1)) == []
    assert repo.due(T0) == [once]

    fire_at = T0 + timedelta(seconds=5)
    assert repo.mark_fired(once.id, fire_at) is True
    fired = repo.get(once.id)
    assert fired.last_fired_at == fire_at and fired.status == "fired"
    assert repo.due(T0 + timedelta(days=1)) == []
    assert repo.mark_fired(once.id, fire_at) is False

    rec = repo.add("cron", due_at=T0, recurrence="daily", next_fire_at=T0)
    nxt = T0 + timedelta(days=1)
    assert repo.mark_fired(rec.id, fire_at, next_fire_at=nxt) is True
    bumped = repo.get(rec.id)
    assert bumped.status == "pending" and bumped.next_fire_at == nxt
    assert bumped.last_fired_at == fire_at and bumped.recurrence == "daily"
    assert repo.due(nxt) == [bumped]
    assert repo.mark_fired("nope", fire_at) is False
    assert repo.get("nope") is None
    assert repo.due(T0 - timedelta(days=1)) == []


# ── 通用 ────────────────────────────────────────────────────────

def test_empty_db_and_unknown_ids_are_safe(db):
    now = T0
    assert SessionRepo(db).get("nope") is None
    assert MessageRepo(db).get("nope") is None
    assert MessageRepo(db).recent() == []
    assert CommitmentRepo(db).get("nope") is None
    assert CommitmentRepo(db).list_open() == []
    assert CommitmentRepo(db).due_open(now) == []
    assert ThreadRepo(db).get("nope") is None
    assert ThreadRepo(db).list_open() == []
    assert OpportunityRepo(db).get("nope") is None
    assert OpportunityRepo(db).list_open(now) == []
    assert DriveRepo(db).for_turn("nope") == []
    assert IntentRepo(db).get("nope") is None
    assert ActionRepo(db).get("nope") is None
    assert ActionRepo(db).for_intent("nope") == []
    assert DeliveryRepo(db).get("nope") is None
    assert ObservationRepo(db).recent() == []
    assert ObservationRepo(db).active(now) == []
    assert MemoryRepo(db).get("nope") is None
    assert MemoryRepo(db).list_active() == []
    assert ScheduleRepo(db).get("nope") is None
    assert ScheduleRepo(db).due(now) == []


def test_rows_are_frozen_dataclasses(db):
    t = ThreadRepo(db).open_thread("x")
    assert dataclasses.is_dataclass(t)
    with pytest.raises(dataclasses.FrozenInstanceError):
        t.subject = "y"
