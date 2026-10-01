"""tests/test_v2_extractor.py — v2 确定性提取器（message.received → 承诺事件）TDD。

Extractor 是旁路消费者：按 event_id 游标（runtime_checkpoints.stream="extractor"）
增量读取 message.received，用确定性规则（日期词 + 事件触发词）产 commitment.created；
逐条消费、同 payload 24h 幂等去重、坏数据逐条跳过并记 stderr——提取失败不上主链。

日期基准：now=2026-10-01 20:00 CST（周四）。时间默认 09:00。
"""
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.extractor import Extractor  # noqa: E402
from chiguo_time import CST  # noqa: E402
from storage.events import EventStore  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

NOW = datetime(2026, 10, 1, 20, 0, tzinfo=CST)  # 周四


def _db(tmp_path, *, checkpoints=True) -> Database:
    db = Database(tmp_path / "chiguo.sqlite")
    migrate(db)
    if not checkpoints:
        with db.transaction() as conn:
            conn.execute("DROP TABLE runtime_checkpoints")
    return db


def _recv(store, text, *, correlation_id=None):
    return store.append("message.received", source="wechat",
                        payload={"text": text, "recv_id": "r1", "analysis": None},
                        occurred_at=NOW, correlation_id=correlation_id)


def _commitments(store, limit=20):
    return store.recent(type="commitment.created", limit=limit)


# ── 日期词 → due_at ──────────────────────────────────────────────

@pytest.mark.parametrize("text,due", [
    ("今天考试", "2026-10-01T09:00:00+08:00"),
    ("明天交作业", "2026-10-02T09:00:00+08:00"),
    ("后天面试", "2026-10-03T09:00:00+08:00"),
    ("大后天答辩", "2026-10-04T09:00:00+08:00"),
    ("3天后体检", "2026-10-04T09:00:00+08:00"),
    ("0天后开会", "2026-10-01T09:00:00+08:00"),
    ("下周一开会", "2026-10-05T09:00:00+08:00"),
    ("下周日面签", "2026-10-11T09:00:00+08:00"),
    ("周五聚", "2026-10-02T09:00:00+08:00"),
    ("周三约饭", "2026-10-07T09:00:00+08:00"),   # 本周三（9/30）已过 → 下周三
    ("周四体检", "2026-10-01T09:00:00+08:00"),   # 今天即周四（未过 → 今天）
    ("周天取快递", "2026-10-04T09:00:00+08:00"),
    ("10月5日报名", "2026-10-05T09:00:00+08:00"),
    ("10月5号缴费", "2026-10-05T09:00:00+08:00"),
    ("9月30日交", "2027-09-30T09:00:00+08:00"),  # 今年已过 → 明年
    ("1月1日纪念日", "2027-01-01T09:00:00+08:00"),
    ("明天还书", "2026-10-02T09:00:00+08:00"),
    # 多个日期词 → 取最左（最先出现）的一个
    ("明天考试，后天再说", "2026-10-02T09:00:00+08:00"),
])
def test_date_words_produce_commitment(tmp_path, text, due):
    db = _db(tmp_path)
    store = EventStore(db)
    _recv(store, text)
    ids = Extractor(db).catch_up(NOW)
    events = _commitments(store)
    assert len(events) == 1
    assert events[0].payload["due_at"] == due
    assert events[0].payload["kind"] == "user_event"
    assert events[0].payload["extracted_by"] == "rule"
    assert ids == [events[0].event_id]


@pytest.mark.parametrize("text", [
    "明天见",          # 有日期无触发词
    "考试了",          # 有触发词无日期
    "今天天气不错",    # 两者都无
    "2月30日考试",     # 非法日期 → 解析失败不产事件
    "下周考试",        # 缺周几 → 不产
])
def test_no_event_when_date_or_trigger_missing(tmp_path, text):
    db = _db(tmp_path)
    store = EventStore(db)
    _recv(store, text)
    assert Extractor(db).catch_up(NOW) == []
    assert _commitments(store) == []


# ── 事件形状 ─────────────────────────────────────────────────────

def test_payload_causation_correlation_shape(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    msg = _recv(store, "明天要交作业。", correlation_id="conv-1")
    ids = Extractor(db).catch_up(NOW)
    assert len(ids) == 1
    ev = store.get(ids[0])
    assert ev.type == "commitment.created"
    assert ev.source == "extractor"
    assert ev.causation_id == msg.event_id
    assert ev.correlation_id == "conv-1"
    assert ev.payload == {
        "kind": "user_event",
        "subject": "要交作业",
        "due_at": "2026-10-02T09:00:00+08:00",
        "details": {"text": "明天要交作业。"},
        "extracted_by": "rule",
    }


def test_subject_cleaning_and_truncation(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    long_tail = "考" + "A" * 60
    _recv(store, "明天" + long_tail)
    _recv(store, "明天要交作业！记得")
    Extractor(db).catch_up(NOW)
    subjects = {e.payload["subject"] for e in _commitments(store)}
    assert long_tail[:40] in subjects        # 去掉日期词、截断 40 字符
    assert "要交作业！记得" in subjects      # 只去首尾标点/空白，不动中间
    assert all(len(s) <= 40 for s in subjects)


# ── 游标 / 幂等 ──────────────────────────────────────────────────

def test_same_message_processed_once_across_instances(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    _recv(store, "明天考试")
    first = Extractor(db).catch_up(NOW)
    assert len(first) == 1
    # 新实例（游标已持久化）不重复消费
    assert Extractor(db).catch_up(NOW) == []
    assert len(_commitments(store)) == 1


def test_duplicate_payload_within_24h_skipped(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    _recv(store, "明天考试")
    _recv(store, "明天考试！")   # 清洗后同 (subject, due_at) → 幂等跳过
    ids = Extractor(db).catch_up(NOW)
    assert len(ids) == 1
    assert len(_commitments(store)) == 1
    # 第二条消息已被消费（游标越过），只是未产事件
    assert Extractor(db).catch_up(NOW) == []


def test_same_payload_after_24h_not_skipped(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    _recv(store, "10月5日考试")
    assert len(Extractor(db).catch_up(NOW)) == 1
    _recv(store, "10月5日考试")
    later = NOW + timedelta(hours=25)
    ids = Extractor(db).catch_up(later)
    assert len(ids) == 1
    assert len(_commitments(store)) == 2


def test_limit_caps_messages_per_round(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    _recv(store, "明天考试")
    _recv(store, "3天后体检")
    _recv(store, "10月5日报名")
    ex = Extractor(db)
    first = ex.catch_up(NOW, limit=2)
    assert len(first) == 2
    second = ex.catch_up(NOW)
    assert len(second) == 1
    assert set(first) | set(second) == {e.event_id for e in _commitments(store)}


def test_missing_checkpoint_table_returns_empty(tmp_path):
    """migration 未跑（表不存在）→ 返回 []，不抛、不产事件。"""
    db = _db(tmp_path, checkpoints=False)
    store = EventStore(db)
    _recv(store, "明天考试")
    assert Extractor(db).catch_up(NOW) == []
    assert _commitments(store) == []


# ── 坏数据容错 ───────────────────────────────────────────────────

def test_corrupt_payload_does_not_crash_and_cursor_advances(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    with db.transaction() as conn:
        for eid, payload in (("000-corrupt", "{not-json"),      # 非法 JSON（排最前）
                             ("fff-corrupt", '{"text": 123}')):  # 合法 JSON 但形状坏
            conn.execute(
                "INSERT INTO events(event_id,type,occurred_at,observed_at,source,"
                "correlation_id,causation_id,payload) VALUES (?,?,?,?,?,?,?,?)",
                (eid, "message.received", NOW.isoformat(), NOW.isoformat(),
                 "wechat", None, None, payload))
    _recv(store, "明天考试")
    ids = Extractor(db).catch_up(NOW)
    assert len(ids) == 1                       # 坏行逐条跳过，后续消息照常提取
    assert Extractor(db).catch_up(NOW) == []   # 游标已越过坏行，不卡死
    assert len(_commitments(store)) == 1


def test_non_text_payload_skipped(tmp_path):
    db = _db(tmp_path)
    store = EventStore(db)
    for payload in ({}, {"text": 123}, {"text": "   "}, {"text": None}):
        store.append("message.received", source="wechat", payload=payload,
                     occurred_at=NOW)
    assert Extractor(db).catch_up(NOW) == []
    assert _commitments(store) == []
