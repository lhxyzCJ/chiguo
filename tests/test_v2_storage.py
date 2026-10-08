"""tests/test_v2_storage.py — Chiguo v2 storage 基座（SQLite + migrations + events）TDD。

覆盖 Phase 2 交付物：schema/migration 幂等与 fail-fast、PRAGMA、事务回滚、
0600 隐私权限、integrity、backup、EventStore 追加/读取/因果链/排序。
"""
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.sqlite.db import Database, StorageError, SchemaTooNewError  # noqa: E402
from storage.sqlite.migrations import MIGRATIONS, SCHEMA_VERSION, migrate  # noqa: E402
from storage.events import Event, EventStore  # noqa: E402

CST = timezone(timedelta(hours=8))


def _db(tmp_path) -> Database:
    return Database(tmp_path / "chiguo.sqlite")


# ── db / migrations ─────────────────────────────────────────────

def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    db = _db(tmp_path)
    applied = migrate(db)
    assert applied == list(range(1, SCHEMA_VERSION + 1))
    assert db.schema_version() == SCHEMA_VERSION
    assert migrate(db) == []          # 二次运行无新迁移
    names = db.table_names()
    for t in ("events", "messages", "commitments", "threads", "opportunities",
              "drives", "intents", "actions", "memories", "schedules",
              "deliveries", "world_observations", "autonomous_turns"):
        assert t in names, f"缺表 {t}"


def test_pragmas_applied(tmp_path):
    db = _db(tmp_path)
    conn = db.connect()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_database_file_is_0600(tmp_path):
    db = _db(tmp_path)
    migrate(db)
    mode = (tmp_path / "chiguo.sqlite").stat().st_mode & 0o777
    assert mode == 0o600, f"隐私数据文件应为 0600，实得 {oct(mode)}"


def test_transaction_rolls_back_on_error(tmp_path):
    db = _db(tmp_path)
    migrate(db)
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO events(event_id,type,occurred_at,observed_at,source,payload)"
                " VALUES ('e1','t','2026-01-01T00:00:00+08:00','2026-01-01T00:00:00+08:00','s','{}')")
            raise RuntimeError("boom")
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_migrate_refuses_future_schema(tmp_path):
    db = _db(tmp_path)
    migrate(db)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO schema_migrations(version,name,checksum,applied_at)"
            " VALUES (999,'future','x','2026-01-01T00:00:00+08:00')")
    with pytest.raises(SchemaTooNewError):
        migrate(Database(tmp_path / "chiguo.sqlite"))


def test_migrate_detects_checksum_drift(tmp_path, monkeypatch):
    import storage.sqlite.migrations as mig
    db = _db(tmp_path)
    migrate(db)
    drifted = tuple(
        mig.Migration(m.version, m.name, m.sql + "\n-- drift") if m.version == 1 else m
        for m in MIGRATIONS)
    monkeypatch.setattr(mig, "MIGRATIONS", drifted)
    with pytest.raises(StorageError):
        mig.migrate(db)


def test_corrupt_database_fails_fast(tmp_path):
    p = tmp_path / "bad.sqlite"
    p.write_bytes(b"definitely not a sqlite database" * 64)
    db = Database(p)
    with pytest.raises(StorageError):
        migrate(db)


def test_integrity_ok(tmp_path):
    db = _db(tmp_path)
    migrate(db)
    r = db.integrity()
    assert r["ok"] is True
    assert r["foreign_key_violations"] == []


def test_backup_copies_database(tmp_path):
    db = _db(tmp_path)
    migrate(db)
    dest = tmp_path / "backups" / "b.sqlite"
    out = db.backup(dest)
    assert out == dest and dest.exists()
    assert Database(dest).schema_version() == SCHEMA_VERSION


# ── EventStore ──────────────────────────────────────────────────

def _store(tmp_path) -> EventStore:
    db = _db(tmp_path)
    migrate(db)
    return EventStore(db)


def test_append_and_get_roundtrip(tmp_path):
    store = _store(tmp_path)
    at = datetime(2026, 10, 1, 9, 0, tzinfo=CST)
    ev = store.append("message.received", source="wechat", payload={"text": "你好"},
                      occurred_at=at, correlation_id="conv-1")
    got = store.get(ev.event_id)
    assert got == ev
    assert got.type == "message.received"
    assert got.source == "wechat"
    assert got.payload == {"text": "你好"}
    assert got.occurred_at == at
    assert got.correlation_id == "conv-1"
    assert got.causation_id is None


def test_event_ids_are_time_ordered(tmp_path):
    store = _store(tmp_path)
    ids = [store.append("wake", source="scheduler").event_id for _ in range(5)]
    assert ids == sorted(ids)


def test_recent_returns_newest_first_with_filters(tmp_path):
    store = _store(tmp_path)
    base = datetime(2026, 10, 1, 0, 0, tzinfo=CST)
    for i in range(3):
        store.append("wake", source="scheduler", occurred_at=base + timedelta(hours=i))
    store.append("message.received", source="wechat", occurred_at=base + timedelta(hours=9))
    recent = store.recent(limit=10)
    assert [e.type for e in recent] == ["message.received", "wake", "wake", "wake"]
    only_wake = store.recent(limit=10, type="wake")
    assert len(only_wake) == 3
    assert all(e.type == "wake" for e in only_wake)


def test_since_returns_ascending_window(tmp_path):
    store = _store(tmp_path)
    base = datetime(2026, 10, 1, 0, 0, tzinfo=CST)
    for i in range(4):
        store.append("wake", source="scheduler", occurred_at=base + timedelta(hours=i))
    window = store.since(base + timedelta(hours=1), until=base + timedelta(hours=3))
    assert [e.occurred_at for e in window] == [base + timedelta(hours=1),
                                               base + timedelta(hours=2)]


def test_chain_walks_causation_to_root(tmp_path):
    store = _store(tmp_path)
    root = store.append("message.received", source="wechat", payload={"text": "明天考试"})
    c1 = store.append("commitment.created", source="reducer", causation_id=root.event_id)
    c2 = store.append("schedule.completed", source="schedule", causation_id=c1.event_id)
    intent = store.append("intent.created", source="planner", causation_id=c2.event_id)
    action = store.append("action.started", source="executor", causation_id=intent.event_id)
    sent = store.append("message.sent", source="wechat", causation_id=action.event_id)
    chain = store.chain(sent.event_id)
    assert [e.event_id for e in chain] == [root.event_id, c1.event_id, c2.event_id,
                                           intent.event_id, action.event_id, sent.event_id]
    # 单节点链
    assert [e.event_id for e in store.chain(root.event_id)] == [root.event_id]
    # 未知 id → 空
    assert store.chain("nope") == []


def test_after_cursor_returns_events_in_commit_order(tmp_path):
    store = _store(tmp_path)
    first = store.append("wake", source="scheduler")
    e2 = store.append("message.received", source="wechat")
    e3 = store.append("wake", source="scheduler")
    got = store.after(first.cursor)
    assert [e.event_id for e in got] == [e2.event_id, e3.event_id]
    assert store.after(e3.cursor) == []
    assert [e.event_id for e in store.after(first.cursor, limit=1)] == [e2.event_id]
    assert store.latest_cursor() == e3.cursor
    assert store.get(e2.event_id).cursor == e2.cursor


def test_cursor_not_poisoned_by_out_of_order_commit(tmp_path):
    """并发乱序提交回归：uuid 更小但提交更晚的事件必须仍被消费。

    uuid7 仅保证生成时的时间趋势——并发进程/线程下可能「先生成、后提交」，
    按 uuid 字典序做游标会永久漏事件（旧实现 bug）；rowid 游标（提交序）
    不受影响。
    """
    from app.runtime.reducer import Reducer
    db = _db(tmp_path)
    migrate(db)
    store = EventStore(db)
    store.append("wake", source="a")
    # 模拟 B 进程：uuid 更小（更早生成）、rowid 更大（更晚提交）
    db.connect().execute(
        "INSERT INTO events(event_id,type,occurred_at,observed_at,source,payload)"
        " VALUES ('00000000000000000000000000000001','wake',"
        " '2026-10-01T00:00:00+08:00','2026-10-01T00:00:00+08:00','b','{}')")
    assert Reducer(db, {}).catch_up() == 2  # uuid 游标会漏掉第二条


def test_migration_v2_checkpoints_table(tmp_path):
    db = _db(tmp_path)
    applied = migrate(db)
    assert 2 in applied
    assert db.schema_version() == SCHEMA_VERSION == 2
    assert "runtime_checkpoints" in db.table_names()


def test_checkpoint_roundtrip(tmp_path):
    from storage.repositories.checkpoints import CheckpointRepo
    db = _db(tmp_path)
    migrate(db)
    repo = CheckpointRepo(db)
    assert repo.get("runtime") is None
    t = datetime(2026, 10, 1, 9, 0, tzinfo=CST)
    repo.save("runtime", last_event_id="e-1", last_occurred_at=t,
              state={"affect": {"loneliness": 42.0}})
    cp = repo.get("runtime")
    assert cp.last_event_id == "e-1"
    assert cp.last_occurred_at == t
    assert cp.state == {"affect": {"loneliness": 42.0}}
    # upsert 覆盖
    repo.save("runtime", last_event_id="e-2", last_occurred_at=t, state={})
    assert repo.get("runtime").last_event_id == "e-2"
    assert repo.get("runtime").state == {}


def test_caused_by_returns_direct_children(tmp_path):
    store = _store(tmp_path)
    root = store.append("message.received", source="wechat")
    c1 = store.append("commitment.created", source="reducer", causation_id=root.event_id)
    c2 = store.append("thread.opened", source="reducer", causation_id=root.event_id)
    child = store.append("intent.created", source="planner", causation_id=c1.event_id)
    children = store.caused_by(root.event_id)
    assert [e.event_id for e in children] == [c1.event_id, c2.event_id]
    assert [e.event_id for e in store.caused_by(c1.event_id)] == [child.event_id]
    assert store.caused_by("nope") == []


def test_concurrent_appends_both_persist(tmp_path):
    store = _store(tmp_path)
    db2 = Database(tmp_path / "chiguo.sqlite")
    store2 = EventStore(db2)
    store.append("wake", source="a")
    store2.append("wake", source="b", occurred_at=datetime(2026, 10, 1, tzinfo=CST) + timedelta(seconds=1))
    assert store.count() == 2
