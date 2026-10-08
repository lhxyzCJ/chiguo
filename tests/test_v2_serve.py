"""tests/test_v2_serve.py — 运行时回环 HTTP（Pi extension 服务端）TDD。"""
import json
import sys
import time
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


def _retry(fn):
    """WSL2 回环偶发 connect 超时（stock ThreadingHTTPServer 也可复现，环境问题）
    → 有界重试；非超时错误直接上抛。"""
    last = None
    for _ in range(8):
        try:
            return fn()
        except urllib.error.URLError as e:
            if not isinstance(e.reason, TimeoutError):
                raise
            last = e
            time.sleep(0.3)
    raise last


def _get(url):
    def once():
        with urllib.request.urlopen(url, timeout=3) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    return _retry(once)


def _post(url, body):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", "Connection": "close"})

    def once():
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
    return _retry(once)


def _server(tmp_path, token=None):
    db = Database(tmp_path / "c.sqlite")
    migrate(db)
    srv = RuntimeServer(db, CONFIG, port=0, token=token)
    srv.start()
    return srv, db


def _get_h(url, token=None):
    req = urllib.request.Request(url, headers={"Connection": "close"})
    if token:
        req.add_header("X-Chiguo-Token", token)

    def once():
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))
    return _retry(once)


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


def _now_ms() -> int:
    return int(datetime.now(CST).timestamp() * 1000)


def test_turn_endpoint_records_user_message(tmp_path):
    srv, db = _server(tmp_path)
    try:
        status, data = _post(f"http://127.0.0.1:{srv.port}/turn",
                             {"session": "chiguo-main", "role": "user",
                              "text": "菓菓在吗", "at": _now_ms()})
        assert status == 200 and data["ok"] is True
        events = EventStore(db).recent(limit=5)
        assert events[0].type == "message.received"
        assert events[0].payload["text"] == "菓菓在吗"
        assert events[0].payload["session"] == "chiguo-main"
        assert abs((events[0].occurred_at - datetime.now(CST)).total_seconds()) < 5
    finally:
        srv.stop()


def test_turn_timestamp_skew_is_clamped(tmp_path, capsys):
    """M4：脏时间戳（秒当 ms/未来/1970）→ 落服务器时间，不污染时间轴。"""
    srv, db = _server(tmp_path)
    try:
        status, _ = _post(f"http://127.0.0.1:{srv.port}/turn",
                          {"session": "s", "role": "user", "text": "秒级时间戳",
                           "at": 1_700_000_000})          # 秒当 ms → 1970 域
        assert status == 200
        status, _ = _post(f"http://127.0.0.1:{srv.port}/turn",
                          {"session": "s", "role": "user", "text": "未来时间",
                           "at": _now_ms() + 100 * 3600 * 1000})
        assert status == 200
        events = EventStore(db).recent(limit=5)
        for ev in events:
            assert abs((ev.occurred_at - datetime.now(CST)).total_seconds()) < 60
        assert "偏差过大" in capsys.readouterr().err
    finally:
        srv.stop()


def test_turn_endpoint_records_assistant_reply_without_affect_burn(tmp_path):
    srv, db = _server(tmp_path)
    try:
        status, _ = _post(f"http://127.0.0.1:{srv.port}/turn",
                          {"session": "chiguo-main", "role": "assistant",
                           "text": "……哼，在的。", "at": _now_ms()})
        assert status == 200
        ev = EventStore(db).recent(limit=1)[0]
        assert ev.type == "conversation.replied"   # 回复不是主动 send，不扣能量
    finally:
        srv.stop()


def test_serve_token_required_when_configured(tmp_path):
    """M5：配置 token 后所有端点要求 X-Chiguo-Token。"""
    srv, db = _server(tmp_path, token="s3cret")
    try:
        status, data = _get_h(f"http://127.0.0.1:{srv.port}/context")
        assert status == 401 and data["ok"] is False
        status, data = _get_h(f"http://127.0.0.1:{srv.port}/context", token="s3cret")
        assert status == 200 and "agenda" in data
        status, _ = _post(f"http://127.0.0.1:{srv.port}/turn",
                          {"role": "user", "text": "x"})
        assert status == 401
    finally:
        srv.stop()


def test_turn_requires_json_content_type(tmp_path):
    """M5：POST /turn 拒绝非 application/json（防浏览器简单请求盲写）。"""
    srv, db = _server(tmp_path)
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/turn",
            data=b'{"role":"user","text":"x"}',
            headers={"Content-Type": "text/plain", "Connection": "close"})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status == 415
        assert EventStore(db).count() == 0
    finally:
        srv.stop()


def test_serve_forbidden_host_helper():
    from app.runtime.serve import _host_ok
    assert _host_ok("127.0.0.1:8790") is True
    assert _host_ok("localhost") is True
    assert _host_ok("[::1]:8790") is True
    assert _host_ok("evil.example.com") is False
    assert _host_ok("") is False


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
