"""storage.repositories.schedules — 调度仓储（wake / deferred work）。

调度只负责「何时触发」；「要不要发消息」的决策不在这里。payload 为 JSON。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Schedule:
    id: str
    kind: str
    due_at: datetime | None
    recurrence: str | None
    payload: dict | None
    status: str
    last_fired_at: datetime | None
    next_fire_at: datetime | None
    created_at: datetime


class ScheduleRepo:
    """schedules 表：add / get / due / mark_fired。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, kind: str, due_at: datetime | None = None,
            recurrence: str | None = None, payload: dict | None = None,
            next_fire_at: datetime | None = None) -> Schedule:
        """新建调度（pending）；next_fire_at 未给时默认取 due_at（一次性）。"""
        s = Schedule(
            id=uuid.uuid7().hex, kind=str(kind), due_at=due_at,
            recurrence=recurrence,
            payload=dict(payload) if payload is not None else None,
            status="pending", last_fired_at=None,
            next_fire_at=next_fire_at if next_fire_at is not None else due_at,
            created_at=datetime.now(CST))
        self.db.connect().execute(
            "INSERT INTO schedules(id, kind, due_at, recurrence, payload, status,"
            " last_fired_at, next_fire_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (s.id, s.kind,
             s.due_at.isoformat() if s.due_at is not None else None,
             s.recurrence,
             (json.dumps(s.payload, ensure_ascii=False)
              if s.payload is not None else None),
             s.status, None,
             s.next_fire_at.isoformat() if s.next_fire_at is not None else None,
             s.created_at.isoformat()))
        return s

    def get(self, schedule_id: str) -> Schedule | None:
        row = self.db.connect().execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        return self._row(row) if row is not None else None

    def due(self, now: datetime) -> list[Schedule]:
        """到点的 pending 调度（next_fire_at <= now，按触发时间升序）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM schedules WHERE status = 'pending'"
            " AND next_fire_at IS NOT NULL AND next_fire_at <= ?"
            " ORDER BY next_fire_at ASC, id ASC", (now.isoformat(),)).fetchall()
        return [self._row(r) for r in rows]

    def mark_fired(self, schedule_id: str, when: datetime,
                   next_fire_at: datetime | None = None) -> bool:
        """记录一次触发；给了 next_fire_at → 保持 pending（循环），否则置 fired。

        仅对 pending 调度有效；不存在/非 pending → False。
        """
        conn = self.db.connect()
        if next_fire_at is None:
            cur = conn.execute(
                "UPDATE schedules SET status = 'fired', last_fired_at = ?"
                " WHERE id = ? AND status = 'pending'",
                (when.isoformat(), schedule_id))
        else:
            cur = conn.execute(
                "UPDATE schedules SET status = 'pending', last_fired_at = ?,"
                " next_fire_at = ? WHERE id = ? AND status = 'pending'",
                (when.isoformat(), next_fire_at.isoformat(), schedule_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Schedule:
        return Schedule(
            id=row["id"], kind=row["kind"],
            due_at=(datetime.fromisoformat(row["due_at"])
                    if row["due_at"] is not None else None),
            recurrence=row["recurrence"],
            payload=(json.loads(row["payload"])
                     if row["payload"] is not None else None),
            status=row["status"],
            last_fired_at=(datetime.fromisoformat(row["last_fired_at"])
                           if row["last_fired_at"] is not None else None),
            next_fire_at=(datetime.fromisoformat(row["next_fire_at"])
                          if row["next_fire_at"] is not None else None),
            created_at=datetime.fromisoformat(row["created_at"]))
