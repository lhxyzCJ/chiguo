"""tests/test_v2_dualwrite.py — Phase 3 事件双写（旧链路 → v2 events）TDD。

双写语义：旧链路照常运行，关键事实旁路写入 v2 SQLite events；
DB 未初始化/未启用/损坏 → 静默跳过（绝不阻断旧链）。
测试中默认禁用（conftest 设 CHIGUO_EVENT_DUALWRITE=0），本文件显式启用。
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import storage.dualwrite as dw  # noqa: E402
from storage.events import EventStore  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

CST = timezone(timedelta(hours=8))


def _init_db(path: Path) -> None:
    migrate(Database(path))


@pytest.fixture
def dw_env(tmp_path, monkeypatch):
    """启用双写并指向隔离 DB；返回 (db_path, EventStore)。"""
    dbp = tmp_path / "events.sqlite"
    _init_db(dbp)
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "1")
    monkeypatch.setenv("CHIGUO_DB_PATH", str(dbp))
    dw.reset_cache()
    yield dbp, EventStore(Database(dbp))
    dw.reset_cache()


def test_message_received_event(dw_env):
    _dbp, store = dw_env
    dw.message_received(text="明天考线代", recv_id="r-1", analysis={"warmth": 0.5})
    events = store.recent(limit=5)
    assert len(events) == 1
    ev = events[0]
    assert ev.type == "message.received"
    assert ev.source == "wechat"
    assert ev.payload["text"] == "明天考线代"
    assert ev.payload["recv_id"] == "r-1"
    assert ev.payload["analysis"] == {"warmth": 0.5}


def test_message_sent_correlates_to_msg_id(dw_env):
    _dbp, store = dw_env
    dw.message_sent(msg_id="m-1", text="记得带伞", trigger="lonely_low",
                    intensity="soft")
    ev = store.recent(limit=1)[0]
    assert ev.type == "message.sent"
    assert ev.correlation_id == "m-1"
    assert ev.payload["text"] == "记得带伞"
    assert ev.payload["trigger"] == "lonely_low"


def test_delivery_events(dw_env):
    _dbp, store = dw_env
    dw.message_sent(msg_id="m-2", text="早", trigger="morning", intensity="soft")
    dw.delivery_failed(msg_id="m-2", error="bridge 500")
    dw.delivery_uncertain(msg_id="m-3", error="timeout_uncertain")
    types = [e.type for e in store.recent(limit=10)]
    assert types == ["message.uncertain", "message.delivery_failed", "message.sent"]
    failed = store.recent(limit=10)[1]
    assert failed.correlation_id == "m-2"
    assert failed.payload["error"] == "bridge 500"
    assert store.recent(limit=10)[0].correlation_id == "m-3"


def test_wake_event(dw_env):
    _dbp, store = dw_env
    dw.wake(action="idle", reason="quiet_hours", msg_id=None)
    ev = store.recent(limit=1)[0]
    assert ev.type == "wake"
    assert ev.source == "scheduler"
    assert ev.payload == {"action": "idle", "reason": "quiet_hours", "msg_id": None}


def test_schedule_change_event(dw_env):
    _dbp, store = dw_env
    dw.schedule_changed(kind="cancel", item={"kind": "cancel", "date": "2026-10-09",
                                             "period": 3}, actor="wechat_command")
    ev = store.recent(limit=1)[0]
    assert ev.type == "schedule.created"
    assert ev.payload["kind"] == "cancel"
    assert ev.payload["item"]["period"] == 3


def test_disabled_by_env_writes_nothing(tmp_path, monkeypatch):
    dbp = tmp_path / "events.sqlite"
    _init_db(dbp)
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "0")
    monkeypatch.setenv("CHIGUO_DB_PATH", str(dbp))
    dw.reset_cache()
    dw.message_received(text="x", recv_id=None, analysis=None)
    assert EventStore(Database(dbp)).count() == 0
    dw.reset_cache()


def test_missing_db_is_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "1")
    monkeypatch.setenv("CHIGUO_DB_PATH", str(tmp_path / "missing" / "x.sqlite"))
    dw.reset_cache()
    dw.message_received(text="x", recv_id=None, analysis=None)  # 不抛
    assert not (tmp_path / "missing" / "x.sqlite").exists()
    dw.reset_cache()


def test_corrupt_db_is_silent(tmp_path, monkeypatch):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"garbage" * 64)
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "1")
    monkeypatch.setenv("CHIGUO_DB_PATH", str(bad))
    dw.reset_cache()
    dw.message_sent(msg_id="m", text="t", trigger="t", intensity="soft")  # 不抛
    dw.reset_cache()


def test_path_resolution_from_toml(tmp_path, monkeypatch):
    """未设 CHIGUO_DB_PATH 时，从 config [storage].db_path 解析（相对路径锚定 _base_dir）。"""
    dbp = tmp_path / "data" / "chiguo.sqlite"
    dbp.parent.mkdir(parents=True)
    _init_db(dbp)
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "1")
    monkeypatch.delenv("CHIGUO_DB_PATH", raising=False)
    dw.reset_cache()
    config = {"_base_dir": str(tmp_path), "storage": {"db_path": "data/chiguo.sqlite"}}
    dw.wake(action="idle", reason="manual", msg_id=None, config=config)
    assert EventStore(Database(dbp)).count() == 1
    dw.reset_cache()


# ── 旧链路接线（真实引擎，tmp 隔离）───────────────────────────

def _make_engine(base: Path):
    from chiguo_daemon import DecisionEngine
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{base / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{base / "no_history.db"}"', src)
    src = re.sub(r"(?m)^db_path\s*=.*$",
                 f'db_path = "{base / "events.sqlite"}"', src)
    cfg_path = base / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return DecisionEngine(str(cfg_path), str(base / "chiguo_decisions.jsonl"))


def test_engine_record_user_message_dualwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "1")
    monkeypatch.setenv("CHIGUO_MEM0_DISABLED", "1")
    monkeypatch.delenv("CHIGUO_DB_PATH", raising=False)
    dw.reset_cache()
    _init_db(tmp_path / "events.sqlite")
    eng = _make_engine(tmp_path)
    eng.record_user_message("菓菓在吗", recv_id="u-1")
    store = EventStore(Database(tmp_path / "events.sqlite"))
    received = [e for e in store.recent(limit=10) if e.type == "message.received"]
    assert len(received) == 1
    assert received[0].payload["text"] == "菓菓在吗"
    dw.reset_cache()


def test_engine_record_send_text_and_result_dualwrites(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIGUO_EVENT_DUALWRITE", "1")
    monkeypatch.setenv("CHIGUO_MEM0_DISABLED", "1")
    monkeypatch.delenv("CHIGUO_DB_PATH", raising=False)
    dw.reset_cache()
    _init_db(tmp_path / "events.sqlite")
    eng = _make_engine(tmp_path)
    eng.record_send_text("m-9", "出门带伞", trigger="lonely_low", intensity="soft")
    eng.record_send_result("m-9", "failed", "bridge down")
    store = EventStore(Database(tmp_path / "events.sqlite"))
    types = [e.type for e in store.recent(limit=10)]
    assert "message.sent" in types
    assert "message.delivery_failed" in types
    dw.reset_cache()
