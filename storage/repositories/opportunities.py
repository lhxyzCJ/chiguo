"""storage.repositories.opportunities — 机会（可行动契机）仓储。

评分（novelty/relevance/urgency/emotional_affordance）是 planner 的输入信号；
expires_at 过期即作废（open 列表自动排除）。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Opportunity:
    id: str
    kind: str
    novelty: float | None
    relevance: float | None
    urgency: float | None
    emotional_affordance: float | None
    expires_at: datetime | None
    observation_event_id: str | None
    status: str
    created_at: datetime
    payload: dict | None


class OpportunityRepo:
    """opportunities 表：add / get / list_open / set_status。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, kind: str, expires_at: datetime | None = None,
            observation_event_id: str | None = None, novelty: float | None = None,
            relevance: float | None = None, urgency: float | None = None,
            emotional_affordance: float | None = None,
            payload: dict | None = None) -> Opportunity:
        o = Opportunity(
            id=uuid.uuid7().hex, kind=str(kind), novelty=novelty,
            relevance=relevance, urgency=urgency,
            emotional_affordance=emotional_affordance, expires_at=expires_at,
            observation_event_id=observation_event_id, status="open",
            created_at=datetime.now(CST),
            payload=dict(payload) if payload is not None else None)
        self.db.connect().execute(
            "INSERT INTO opportunities(id, kind, novelty, relevance, urgency,"
            " emotional_affordance, expires_at, observation_event_id, status,"
            " created_at, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (o.id, o.kind, o.novelty, o.relevance, o.urgency,
             o.emotional_affordance,
             o.expires_at.isoformat() if o.expires_at is not None else None,
             o.observation_event_id, o.status, o.created_at.isoformat(),
             (json.dumps(o.payload, ensure_ascii=False)
              if o.payload is not None else None)))
        return o

    def get(self, opportunity_id: str) -> Opportunity | None:
        row = self.db.connect().execute(
            "SELECT * FROM opportunities WHERE id = ?", (opportunity_id,)).fetchone()
        return self._row(row) if row is not None else None

    def list_open(self, now: datetime | None = None) -> list[Opportunity]:
        """status=open 且未过期（expires_at 为 NULL 视为不过期），时间升序。"""
        now = now or datetime.now(CST)
        rows = self.db.connect().execute(
            "SELECT * FROM opportunities WHERE status = 'open'"
            " AND (expires_at IS NULL OR expires_at > ?)"
            " ORDER BY created_at ASC, id ASC", (now.isoformat(),)).fetchall()
        return [self._row(r) for r in rows]

    def set_status(self, opportunity_id: str, status: str) -> bool:
        """状态迁移（open/consumed/expired/dismissed）；不存在 → False。"""
        cur = self.db.connect().execute(
            "UPDATE opportunities SET status = ? WHERE id = ?",
            (str(status), opportunity_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Opportunity:
        return Opportunity(
            id=row["id"], kind=row["kind"], novelty=row["novelty"],
            relevance=row["relevance"], urgency=row["urgency"],
            emotional_affordance=row["emotional_affordance"],
            expires_at=(datetime.fromisoformat(row["expires_at"])
                        if row["expires_at"] is not None else None),
            observation_event_id=row["observation_event_id"], status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            payload=(json.loads(row["payload"])
                     if row["payload"] is not None else None))
