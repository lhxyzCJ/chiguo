"""storage.repositories.actions — 行动（actions）与投递（deliveries）仓储。

Action 是执行单元（send_message 只是其中一种）；`input`/`output` 为 JSON。
终态（completed/failed/cancelled）自动填 completed_at，started 自动填 started_at。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database

_TERMINAL = ("completed", "failed", "cancelled")


@dataclass(frozen=True)
class Action:
    id: str
    type: str
    status: str
    intent_id: str | None
    cause_event_id: str | None
    correlation_id: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    input: dict | None
    output: dict | None
    error: str | None


@dataclass(frozen=True)
class Delivery:
    id: str
    action_id: str | None
    message_id: str | None
    channel: str
    status: str
    attempts: int
    sent_at: datetime | None
    error: str | None
    provider_message_id: str | None


class ActionRepo:
    """actions 表：add / get / set_status / for_intent。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, type: str, intent_id: str | None = None,
            cause_event_id: str | None = None, correlation_id: str | None = None,
            input: dict | None = None) -> Action:
        a = Action(id=uuid.uuid7().hex, type=str(type), status="pending",
                   intent_id=intent_id, cause_event_id=cause_event_id,
                   correlation_id=correlation_id, created_at=datetime.now(CST),
                   started_at=None, completed_at=None,
                   input=dict(input) if input is not None else None,
                   output=None, error=None)
        self.db.connect().execute(
            "INSERT INTO actions(id, type, status, intent_id, cause_event_id,"
            " correlation_id, created_at, started_at, completed_at, input, output,"
            " error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (a.id, a.type, a.status, a.intent_id, a.cause_event_id, a.correlation_id,
             a.created_at.isoformat(), None, None,
             (json.dumps(a.input, ensure_ascii=False)
              if a.input is not None else None), None, None))
        return a

    def get(self, action_id: str) -> Action | None:
        row = self.db.connect().execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
        return self._row(row) if row is not None else None

    def set_status(self, action_id: str, status: str,
                   output: dict | None = None, error: str | None = None) -> bool:
        """状态迁移；status=started 补 started_at，终态补 completed_at。

        output/error 仅在显式传入（非 None）时写入；不存在 → False。
        """
        now = datetime.now(CST).isoformat()
        cur = self.db.connect().execute(
            "UPDATE actions SET status = ?,"
            " started_at = CASE WHEN ? = 'started' AND started_at IS NULL"
            "   THEN ? ELSE started_at END,"
            " completed_at = CASE WHEN ? IN ('completed', 'failed', 'cancelled')"
            "   AND completed_at IS NULL THEN ? ELSE completed_at END,"
            " output = COALESCE(?, output), error = COALESCE(?, error)"
            " WHERE id = ?",
            (str(status), str(status), now, str(status), now,
             (json.dumps(output, ensure_ascii=False) if output is not None else None),
             error, action_id))
        return cur.rowcount > 0

    def for_intent(self, intent_id: str) -> list[Action]:
        """某意图下的全部行动（创建时间升序）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM actions WHERE intent_id = ?"
            " ORDER BY created_at ASC, id ASC", (intent_id,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Action:
        return Action(
            id=row["id"], type=row["type"], status=row["status"],
            intent_id=row["intent_id"], cause_event_id=row["cause_event_id"],
            correlation_id=row["correlation_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=(datetime.fromisoformat(row["started_at"])
                        if row["started_at"] is not None else None),
            completed_at=(datetime.fromisoformat(row["completed_at"])
                          if row["completed_at"] is not None else None),
            input=(json.loads(row["input"]) if row["input"] is not None else None),
            output=(json.loads(row["output"]) if row["output"] is not None else None),
            error=row["error"])


class DeliveryRepo:
    """deliveries 表：add / get / set_status。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, action_id: str, channel: str, status: str,
            message_id: str | None = None) -> Delivery:
        d = Delivery(id=uuid.uuid7().hex, action_id=action_id, message_id=message_id,
                     channel=str(channel), status=str(status), attempts=1,
                     sent_at=datetime.now(CST) if status == "sent" else None,
                     error=None, provider_message_id=None)
        self.db.connect().execute(
            "INSERT INTO deliveries(id, action_id, message_id, channel, status,"
            " attempts, sent_at, error, provider_message_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (d.id, d.action_id, d.message_id, d.channel, d.status, d.attempts,
             d.sent_at.isoformat() if d.sent_at is not None else None, None, None))
        return d

    def get(self, delivery_id: str) -> Delivery | None:
        row = self.db.connect().execute(
            "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)).fetchone()
        return self._row(row) if row is not None else None

    def set_status(self, delivery_id: str, status: str, error: str | None = None,
                   provider_message_id: str | None = None) -> bool:
        """投递状态迁移（sent/failed/uncertain）；首次 sent 补 sent_at。

        error/provider_message_id 仅在显式传入（非 None）时写入；不存在 → False。
        """
        now = datetime.now(CST).isoformat()
        cur = self.db.connect().execute(
            "UPDATE deliveries SET status = ?,"
            " sent_at = CASE WHEN ? = 'sent' AND sent_at IS NULL THEN ?"
            "   ELSE sent_at END,"
            " error = COALESCE(?, error),"
            " provider_message_id = COALESCE(?, provider_message_id)"
            " WHERE id = ?",
            (str(status), str(status), now, error, provider_message_id, delivery_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Delivery:
        return Delivery(
            id=row["id"], action_id=row["action_id"], message_id=row["message_id"],
            channel=row["channel"], status=row["status"],
            attempts=row["attempts"],
            sent_at=(datetime.fromisoformat(row["sent_at"])
                     if row["sent_at"] is not None else None),
            error=row["error"], provider_message_id=row["provider_message_id"])
