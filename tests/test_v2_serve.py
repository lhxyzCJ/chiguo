"""tests/test_v2_serve.py — 运行时回环 HTTP（Pi extension 服务端）TDD。"""
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.runtime.serve import RuntimeServer  # noqa: E402
from storage.events import EventStore  # noqa: E402
from storage.repositories.commitments import CommitmentRepo  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

CST = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 1, 20, 0, tzinfo=CST)
CONFIG = {"emotion": {}, "schedule": {"quiet_start": 0, "quiet_end": 8}}


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def _server(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    migrate(db)
    srv = RuntimeServer(db, CONFIG, port=0)
    srv.start()
    return srv, db


def test_context_endpoint(tmp_path):
    srv, db = _server(tmp_path)
    try:
        store = EventStore(db)
        store.append("commitment.created", source="extractor", occurred_at=T0,
                     payload={"kind": "user_event", "subject": "线代考试",
                              "due_at": (T0 + timedelta(days=1)).isoformat()})
        from app.runtime.reducer import Reducer
        Reducer(db, CONFIG).catch_up()
        status, data = _get(f"http://127.0.0.1:{srv.port}/context?session=chiguo-main")
        assert status == 200
        assert "agenda" in data
        assert any("线代考试" in line for line in data["agenda"])
        assert "relationship" in data and "world" in data and "personality" in data
    finally:
        srv.stop()


def test_turn_endpoint_records_user_message(tmp_path):
    srv, db = _server(tmp_path)
    try:
        at_ms = int(T0.timestamp() * 1000)
        status, data = _post(f"http://127.0.0.1:{srv.port}/turn",
                             {"session": "chiguo-main", "role": "user",
                              "text": "菓菓在吗", "at": at_ms})
        assert status == 200 and data["ok"] is True
        events = EventStore(db).recent(limit=5)
        assert events[0].type == "message.received"
        assert events[0].payload["text"] == "菓菓在吗"
        assert events[0].payload["session"] == "chiguo-main"
        assert events[0].occurred_at == T0
    finally:
        srv.stop()


def test_turn_endpoint_records_assistant_reply_without_affect_burn(tmp_path):
    srv, db = _server(tmp_path)
    try:
        status, _ = _post(f"http://127.0.0.1:{srv.port}/turn",
                          {"session": "chiguo-main", "role": "assistant",
                           "text": "……哼，在的。", "at": int(T0.timestamp() * 1000)})
        assert status == 200
        ev = EventStore(db).recent(limit=1)[0]
        assert ev.type == "conversation.replied"   # 回复不是主动 send，不扣能量
    finally:
        srv.stop()


def test_turn_endpoint_bad_payload(tmp_path):
    srv, db = _server(tmp_path)
    try:
        status, data = _post(f"http://127.0.0.1:{srv.port}/turn", {"role": "user"})
        assert status == 400 and data["ok"] is False
        assert EventStore(db).count() == 0
    finally:
        srv.stop()


def test_tick_cli_shadow(tmp_path, capsys):
    """chiguo tick 默认 shadow：跑一个 autonomous turn，不发送。"""
    from app.cli import main
    cfg = tmp_path / "chiguo_proactive.toml"
    cfg.write_text('[emotion]\n[schedule]\nquiet_start = 0\nquiet_end = 8\n'
                   '[storage]\ndb_path = "c.sqlite"\n[netease]\nenabled = false\n'
                   '[weather]\nenabled = false\n', encoding="utf-8")
    dbp = tmp_path / "c.sqlite"
    assert main(["db", "migrate", "--db", str(dbp)]) == 0
    capsys.readouterr()
    rc = main(["tick", "--db", str(dbp), "--config", str(cfg),
               "--now", "2026-10-01T20:00:00+08:00"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["action"] == "tick"
    assert out["outcome"] in ("waited", "deferred", "intent")
    assert "sent" not in out or out.get("sent") is None
