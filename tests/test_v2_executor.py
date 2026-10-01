"""tests/test_v2_executor.py — Action 执行器（send_message 送达闭环）TDD。

生成（Pi/agent-run）与发送（bridge /send）均依赖注入，测试零网络。
产出事件链：action.completed/failed → message.sent / message.delivery_failed。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.actions.executor import (  # noqa: E402
    ExecOutcome, build_generation_payload, execute_action,
)
from storage.events import EventStore  # noqa: E402
from storage.repositories.actions import ActionRepo, DeliveryRepo  # noqa: E402
from storage.repositories.drives import IntentRepo  # noqa: E402
from storage.repositories.messages import MessageRepo  # noqa: E402
from storage.sqlite.db import Database  # noqa: E402
from storage.sqlite.migrations import migrate  # noqa: E402

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 3, 15, 0, tzinfo=CST)


def _setup(tmp_path):
    db = Database(tmp_path / "c.sqlite")
    migrate(db)
    return db


def _make_action(db, **kw):
    intent = IntentRepo(db).add(kw.pop("intent_type", "follow_up"),
                                why={"opportunity_kind": "commitment_due"},
                                plan={"primary": {"subject": "线代考试"}})
    action = ActionRepo(db).add("send_message", intent_id=intent.id,
                                correlation_id="corr-1")
    return action, intent


def test_execute_send_success(tmp_path):
    db = _setup(tmp_path)
    action, intent = _make_action(db)
    sent = {}

    def fake_generator(payload, config):
        return "考得怎么样呀……才、才不是特意关心的。"

    def fake_sender(text, config):
        sent["text"] = text
        return {"ok": True}

    out = execute_action(db, action.id, config={}, now=NOW,
                         generator=fake_generator, sender=fake_sender)
    assert isinstance(out, ExecOutcome) and out.ok is True
    assert sent["text"].startswith("考得怎么样")
    # 状态：action completed；delivery sent；消息归档；事件链
    assert ActionRepo(db).get(action.id).status == "completed"
    assert MessageRepo(db).recent(limit=5)[0].text.startswith("考得怎么样")
    store = EventStore(db)
    types = [e.type for e in store.recent(limit=10)]
    assert "message.sent" in types
    # message.sent 与 action 关联（correlation=action.correlation_id）
    msg_ev = next(e for e in store.recent(limit=10) if e.type == "message.sent")
    assert msg_ev.correlation_id == "corr-1"
    assert msg_ev.payload["intent_id"] == intent.id


def test_execute_generation_failure(tmp_path):
    db = _setup(tmp_path)
    action, _ = _make_action(db)

    out = execute_action(db, action.id, config={}, now=NOW,
                         generator=lambda p, c: None,
                         sender=lambda t, c: {"ok": True})
    assert out.ok is False and "generate" in out.error
    assert ActionRepo(db).get(action.id).status == "failed"
    types = [e.type for e in EventStore(db).recent(limit=10)]
    assert "action.failed" in types
    assert "message.sent" not in types


def test_execute_send_failure_records_delivery(tmp_path):
    db = _setup(tmp_path)
    action, _ = _make_action(db)

    out = execute_action(db, action.id, config={}, now=NOW,
                         generator=lambda p, c: "哼。",
                         sender=lambda t, c: {"ok": False, "error": "bridge 500"})
    assert out.ok is False
    assert ActionRepo(db).get(action.id).status == "failed"
    types = [e.type for e in EventStore(db).recent(limit=10)]
    assert "message.delivery_failed" in types
    # delivery 行记录失败
    conn = db.connect()
    row = conn.execute("SELECT * FROM deliveries").fetchone()
    assert row["status"] == "failed" and row["error"] == "bridge 500"


def test_execute_unknown_or_non_pending_action(tmp_path):
    db = _setup(tmp_path)
    action, _ = _make_action(db)
    ActionRepo(db).set_status(action.id, "completed")
    out = execute_action(db, action.id, config={}, now=NOW,
                         generator=lambda p, c: "x", sender=lambda t, c: {"ok": True})
    assert out.ok is False and "not pending" in out.error
    assert execute_action(db, "nope", config={}, now=NOW,
                          generator=lambda p, c: "x",
                          sender=lambda t, c: {"ok": True}).ok is False


def test_build_generation_payload_has_personality_frame(tmp_path):
    db = _setup(tmp_path)
    _, intent = _make_action(db)
    payload = build_generation_payload(intent, config={}, now=NOW)
    assert payload["action"] == "send"
    assert payload["context"]["intent_type"] == "follow_up"
    # agent-run send-mode 体裁所需字段
    assert "instruction" in payload["context"]
    assert "layer_guidance" in payload["context"]
