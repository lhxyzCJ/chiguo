"""storage.repositories.commitments — 承诺仓储（一等公民，未完成事项）。"""
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Commitment:
    id: str
    kind: str
    subject: str
    details: str | None
    due_at: datetime | None
    status: str
    created_from_event: str | None
    created_at: datetime
    resolved_at: datetime | None
    resolution_event: str | None


class CommitmentRepo:
    """commitments 表：add / get / list_open / resolve / due_open。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, kind: str, subject: str, due_at: datetime | None = None,
            details: str | None = None,
            created_from_event: str | None = None) -> Commitment:
        c = Commitment(
            id=uuid.uuid7().hex, kind=str(kind), subject=str(subject),
            details=details, due_at=due_at, status="open",
            created_from_event=created_from_event, created_at=datetime.now(CST),
            resolved_at=None, resolution_event=None)
        self.db.connect().execute(
            "INSERT INTO commitments(id, kind, subject, details, due_at, status,"
            " created_from_event, created_at, resolved_at, resolution_event)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (c.id, c.kind, c.subject, c.details,
             c.due_at.isoformat() if c.due_at is not None else None, c.status,
             c.created_from_event, c.created_at.isoformat(), None, None))
        return c

    def get(self, commitment_id: str) -> Commitment | None:
        row = self.db.connect().execute(
            "SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
        return self._row(row) if row is not None else None

    def list_open(self) -> list[Commitment]:
        """全部 open（有 due 的按到期升序在前，无 due 的在后）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM commitments WHERE status = 'open'"
            " ORDER BY (due_at IS NULL), due_at ASC, created_at ASC").fetchall()
        return [self._row(r) for r in rows]

    def due_open(self, now: datetime) -> list[Commitment]:
        """已到期且仍 open（due_at <= now，按到期升序）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM commitments WHERE status = 'open' AND due_at IS NOT NULL"
            " AND due_at <= ? ORDER BY due_at ASC, created_at ASC",
            (now.isoformat(),)).fetchall()
        return [self._row(r) for r in rows]

    def resolve(self, commitment_id: str, resolved_at: datetime,
                resolution_event: str | None = None) -> bool:
        """标记完成（open → done）；不存在/已 resolve → False（不覆盖历史）。"""
        cur = self.db.connect().execute(
            "UPDATE commitments SET status = 'done', resolved_at = ?,"
            " resolution_event = ? WHERE id = ? AND status = 'open'",
            (resolved_at.isoformat(), resolution_event, commitment_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Commitment:
        return Commitment(
            id=row["id"], kind=row["kind"], subject=row["subject"],
            details=row["details"],
            due_at=(datetime.fromisoformat(row["due_at"])
                    if row["due_at"] is not None else None),
            status=row["status"], created_from_event=row["created_from_event"],
            created_at=datetime.fromisoformat(row["created_at"]),
            resolved_at=(datetime.fromisoformat(row["resolved_at"])
                         if row["resolved_at"] is not None else None),
            resolution_event=row["resolution_event"])
