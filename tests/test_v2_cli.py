"""tests/test_v2_cli.py — v2 CLI（chiguo db ...）TDD。"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.cli import main  # noqa: E402
from storage.sqlite.migrations import SCHEMA_VERSION  # noqa: E402


def _run(capsys, *argv):
    rc = main(list(argv))
    captured = capsys.readouterr()
    return rc, captured


def test_db_migrate_then_status(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    rc, cap = _run(capsys, "db", "migrate", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "db_migrate"
    assert out["applied"] == list(range(1, SCHEMA_VERSION + 1))

    rc, cap = _run(capsys, "db", "status", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "db_status"
    assert out["ok"] is True
    assert out["initialized"] is True
    assert out["schema_version"] == SCHEMA_VERSION
    assert out["code_schema_version"] == SCHEMA_VERSION
    assert out["path"] == str(dbp)
    assert "events" in out["counts"]


def test_db_migrate_is_idempotent(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "db", "migrate", "--db", str(dbp))
    assert rc == 0
    assert json.loads(cap.out)["applied"] == []


def test_db_status_uninitialized_does_not_create_file(tmp_path, capsys):
    dbp = tmp_path / "missing" / "c.sqlite"
    rc, cap = _run(capsys, "db", "status", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["initialized"] is False
    assert out["schema_version"] == 0
    assert not dbp.exists(), "status 不应有创建数据库的副作用"


def test_db_integrity(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "db", "integrity", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["ok"] is True
    assert out["foreign_key_violations"] == []


def test_db_backup_default_dest(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "db", "backup", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    dest = Path(out["backup_path"])
    assert dest.exists()
    assert dest.parent == tmp_path / "backups"
    # 备份可打开且 schema 一致
    rc, cap = _run(capsys, "db", "status", "--db", str(dest))
    assert json.loads(cap.out)["schema_version"] == SCHEMA_VERSION


def test_db_status_corrupt_file_fails(tmp_path, capsys):
    bad = tmp_path / "bad.sqlite"
    bad.write_bytes(b"garbage" * 128)
    rc, cap = _run(capsys, "db", "status", "--db", str(bad))
    assert rc == 1
    out = json.loads(cap.out)
    assert out["ok"] is False
    assert "error" in out


def test_unknown_command_exits_2(tmp_path, capsys):
    with pytest.raises(SystemExit) as ei:
        main(["db", "nope"])
    assert ei.value.code == 2


# ── events 子命令 ────────────────────────────────────────────────

def _seed_chain(capsys, dbp):
    from storage.events import EventStore
    from storage.sqlite.db import Database
    _run(capsys, "db", "migrate", "--db", str(dbp))
    store = EventStore(Database(dbp))
    root = store.append("message.received", source="wechat", payload={"text": "明天考试"})
    c1 = store.append("commitment.created", source="reducer", causation_id=root.event_id)
    c2 = store.append("message.sent", source="wechat", causation_id=c1.event_id,
                      payload={"text": "加油"})
    return root, c1, c2


def test_events_recent(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    root, c1, c2 = _seed_chain(capsys, dbp)
    rc, cap = _run(capsys, "events", "recent", "--db", str(dbp), "--limit", "10")
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "events_recent"
    assert [e["event_id"] for e in out["events"]] == [c2.event_id, c1.event_id, root.event_id]
    assert out["events"][0]["type"] == "message.sent"
    # type 过滤
    rc, cap = _run(capsys, "events", "recent", "--db", str(dbp), "--type", "commitment.created")
    out = json.loads(cap.out)
    assert [e["type"] for e in out["events"]] == ["commitment.created"]


def test_events_show_with_chain(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    root, c1, c2 = _seed_chain(capsys, dbp)
    rc, cap = _run(capsys, "events", "show", c2.event_id, "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "events_show"
    assert out["event"]["event_id"] == c2.event_id
    assert [e["event_id"] for e in out["chain"]] == [root.event_id, c1.event_id, c2.event_id]
    assert out["caused_by"] == []


def test_events_show_unknown_id_fails(tmp_path, capsys):
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "events", "show", "nope", "--db", str(dbp))
    assert rc == 1
    assert json.loads(cap.out)["ok"] is False


# ── commitments / threads 读取命令 ───────────────────────────────

def test_commitments_cli(tmp_path, capsys):
    from storage.repositories.commitments import CommitmentRepo
    from storage.sqlite.db import Database
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    repo = CommitmentRepo(Database(dbp))
    c = repo.add("user_event", "线代考试",
             due_at=datetime(2026, 10, 3, 9, 0, tzinfo=timezone(timedelta(hours=8))))
    rc, cap = _run(capsys, "commitments", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "commitments"
    assert out["count"] == 1
    assert out["commitments"][0]["id"] == c.id
    assert out["commitments"][0]["subject"] == "线代考试"
    repo.resolve(c.id, datetime(2026, 10, 3, 18, 0, tzinfo=timezone(timedelta(hours=8))))
    rc, cap = _run(capsys, "commitments", "--db", str(dbp))
    assert json.loads(cap.out)["count"] == 0


def test_threads_cli(tmp_path, capsys):
    from storage.repositories.threads import ThreadRepo
    from storage.sqlite.db import Database
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    repo = ThreadRepo(Database(dbp))
    t = repo.open_thread("线代考试", source="conversation")
    rc, cap = _run(capsys, "threads", "--db", str(dbp))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["count"] == 1
    assert out["threads"][0]["id"] == t.id
    repo.close(t.id, datetime(2026, 10, 3, 18, 0, tzinfo=timezone(timedelta(hours=8))))
    rc, cap = _run(capsys, "threads", "--db", str(dbp))
    assert json.loads(cap.out)["count"] == 0


# ── chiguo status 聚合视图 ──────────────────────────────────────

def test_status_aggregates_state(tmp_path, capsys):
    from storage.events import EventStore
    from storage.sqlite.db import Database
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    store = EventStore(Database(dbp))
    store.append("message.received", source="wechat", payload={"text": "在吗"})
    store.append("commitment.created", source="extractor",
                 payload={"kind": "user_event", "subject": "线代考试",
                          "due_at": "2026-10-03T09:00:00+08:00"})
    (tmp_path / "agent_health.json").write_text(
        json.dumps({"state": "up", "fail_streak": 0}), encoding="utf-8")
    count_before = store.count()

    cfg = tmp_path / "chiguo_proactive.toml"
    cfg.write_text('[storage]\ndb_path = "c.sqlite"\n', encoding="utf-8")
    rc, cap = _run(capsys, "status", "--db", str(dbp), "--config", str(cfg))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "status"
    assert out["db"]["schema_version"] == 2
    assert out["affect"]["loneliness"] == 15.0          # 初始态（未消费）
    assert isinstance(out["relationship"]["closeness"], float)
    assert out["commitments"]["count"] == 0             # 未消费事件 → 投影未更新
    assert "last_turn" in out and out["last_turn"] is None
    assert "last_message_sent" in out
    assert out["agent_health"]["state"] == "up"         # 旧健康文件只读展示（迁移期）
    # status 只读：不消费事件
    assert store.count() == count_before


def test_status_after_catch_up_reflects_projection(tmp_path, capsys):
    from app.runtime.reducer import Reducer
    from storage.events import EventStore
    from storage.sqlite.db import Database
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    db = Database(dbp)
    EventStore(db).append("commitment.created", source="extractor",
                          payload={"kind": "user_event", "subject": "体检",
                                   "due_at": "2026-10-08T09:00:00+08:00"})
    Reducer(db, {"emotion": {}}).catch_up()
    rc, cap = _run(capsys, "status", "--db", str(dbp))
    out = json.loads(cap.out)
    assert out["commitments"]["count"] == 1
    assert out["commitments"]["items"][0]["subject"] == "体检"


# ── chiguo autonomous-turn（shadow 自主回合）────────────────────

def _write_toml(tmp_path) -> Path:
    cfg = tmp_path / "chiguo_proactive.toml"
    cfg.write_text(
        '[emotion]\n'
        '[schedule]\nquiet_start = 0\nquiet_end = 8\n'
        '[storage]\ndb_path = "c.sqlite"\n'
        '[netease]\nenabled = false\n'
        '[weather]\nenabled = false\n'
        '[planning]\n', encoding="utf-8")
    return cfg


def test_autonomous_turn_cli_shadow(tmp_path, capsys):
    cfg = _write_toml(tmp_path)
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    rc, cap = _run(capsys, "autonomous-turn", "--db", str(dbp), "--config", str(cfg))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "autonomous_turn"
    assert out["outcome"] in ("waited", "deferred", "intent")
    assert out["turn_id"]
    # shadow：不产生 delivery / 不发送
    conn = _open(dbp)
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_autonomous_turn_cli_execute_creates_action_but_does_not_send(tmp_path, capsys):
    """--execute：有意图时创建 pending action 行；执行仍由 chiguo execute 承担。"""
    cfg = _write_toml(tmp_path)
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    _open(dbp).execute(
        "INSERT INTO commitments(id, kind, subject, due_at, status, created_at)"
        " VALUES ('c1','user_event','线代考试','2026-10-01T09:00:00+08:00','open',"
        " '2026-10-01T00:00:00+08:00')")
    rc, cap = _run(capsys, "autonomous-turn", "--execute",
                   "--now", "2026-10-01T20:00:00+08:00",
                   "--db", str(dbp), "--config", str(cfg))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["outcome"] == "action_pending"
    conn = _open(dbp)
    row = conn.execute("SELECT * FROM actions").fetchone()
    assert row["status"] == "pending" and row["type"] == "send_message"
    assert conn.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def _open(path):
    import sqlite3
    conn = sqlite3.connect(str(path), isolation_level=None)  # autocommit（与生产一致）
    conn.row_factory = sqlite3.Row
    return conn


# ── chiguo execute（执行 pending action）────────────────────────

def test_execute_dry_run_prints_prompt(tmp_path, capsys):
    from storage.repositories.actions import ActionRepo
    from storage.repositories.drives import IntentRepo
    from storage.sqlite.db import Database
    cfg = _write_toml(tmp_path)
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    db = Database(dbp)
    intent = IntentRepo(db).add("follow_up", why={"opportunity_kind": "commitment_due"},
                                plan={"primary": {"subject": "线代考试"}})
    action = ActionRepo(db).add("send_message", intent_id=intent.id)
    rc, cap = _run(capsys, "execute", action.id, "--dry-run",
                   "--db", str(dbp), "--config", str(cfg))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "execute" and out["dry_run"] is True
    assert out["prompt"]["context"]["intent_type"] == "follow_up"


def test_execute_cli_rejects_non_pending(tmp_path, capsys):
    from storage.repositories.actions import ActionRepo
    from storage.repositories.drives import IntentRepo
    from storage.sqlite.db import Database
    cfg = _write_toml(tmp_path)
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    db = Database(dbp)
    intent = IntentRepo(db).add("share", why={}, plan={})
    action = ActionRepo(db).add("send_message", intent_id=intent.id)
    ActionRepo(db).set_status(action.id, "completed")
    rc, cap = _run(capsys, "execute", action.id, "--db", str(dbp), "--config", str(cfg))
    assert rc == 1
    assert json.loads(cap.out)["ok"] is False


# ── chiguo replay ───────────────────────────────────────────────

def test_replay_cli(tmp_path, capsys):
    from storage.events import EventStore
    from storage.sqlite.db import Database
    cfg = _write_toml(tmp_path)
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    store = EventStore(Database(dbp))
    tz = timezone(timedelta(hours=8))
    store.append("commitment.created", source="extractor",
                 occurred_at=datetime(2026, 10, 1, 9, 0, tzinfo=tz),
                 payload={"kind": "user_event", "subject": "线代考试",
                          "due_at": "2026-10-02T09:00:00+08:00"})
    store.append("wake", source="scheduler",
                 occurred_at=datetime(2026, 10, 2, 20, 0, tzinfo=tz))
    rc, cap = _run(capsys, "replay", "--since", "2026-10-01T00:00:00+08:00",
                   "--until", "2026-10-03T00:00:00+08:00",
                   "--db", str(dbp), "--config", str(cfg))
    assert rc == 0
    out = json.loads(cap.out)
    assert out["action"] == "replay" and out["count"] == 1
    assert out["decisions"][0]["outcome"] == "intent"
    assert out["decisions"][0]["intent_type"] == "follow_up"


def test_db_path_env_override(tmp_path, capsys, monkeypatch):
    """M6：env CHIGUO_DB_PATH 与 dualwrite 同源（--db > env > toml > 默认）。"""
    dbp = tmp_path / "env.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    monkeypatch.setenv("CHIGUO_DB_PATH", str(dbp))
    monkeypatch.chdir(tmp_path)  # 防误读仓库 toml
    rc, cap = _run(capsys, "status")
    out = json.loads(cap.out)
    assert rc == 0 and out["db"]["path"] == str(dbp)


def test_unmigrated_db_is_json_error(tmp_path, capsys):
    """M9：存在但未迁移的库 → JSON error + exit 1（不裸 traceback）。"""
    empty = tmp_path / "empty.sqlite"
    empty.write_bytes(b"")
    rc, cap = _run(capsys, "status", "--db", str(empty))
    assert rc == 1
    out = json.loads(cap.out)
    assert out["ok"] is False and "error" in out


def test_tick_execute_generation_failure_exits_1(tmp_path, capsys, monkeypatch):
    """M8：--execute 发送失败 → exit 1（cron 可见）；shadow 恒 0。"""
    cfg = _write_toml(tmp_path)
    dbp = tmp_path / "c.sqlite"
    _run(capsys, "db", "migrate", "--db", str(dbp))
    _open(dbp).execute(
        "INSERT INTO commitments(id, kind, subject, due_at, status, created_at)"
        " VALUES ('c1','user_event','线代考试','2026-09-14T09:00:00+08:00','open',"
        " '2026-09-14T00:00:00+08:00')")
    monkeypatch.setenv("AGENT_RUN_SCRIPT", str(tmp_path / "missing-agent-run.mjs"))
    rc, cap = _run(capsys, "tick", "--execute",
                   "--now", "2026-09-14T20:00:00+08:00",
                   "--db", str(dbp), "--config", str(cfg))
    out = json.loads(cap.out)
    assert rc == 1 and out["ok"] is False
    assert out["sent"]["ok"] is False
    assert out["action_id"]  # action 已记失败终态
