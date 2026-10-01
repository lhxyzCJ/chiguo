"""storage.repositories.threads — 语义线索（threads）仓储。

payload 为 JSON（自由形状），仓储边界做 dict ⇄ TEXT 转换。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Thread:
    id: str
    subject: str
    state: str
    opened_at: datetime
    last_interaction_at: datetime | None
    closed_at: datetime | None
    source: str | None
    payload: dict | None


class ThreadRepo:
    """threads 表：open_thread / get / list_open / touch / close。"""

    def __init__(self, db: Database):
        self.db = db

    def open_thread(self, subject: str, source: str | None = None) -> Thread:
        t = Thread(id=uuid.uuid7().hex, subject=str(subject), state="open",
                   opened_at=datetime.now(CST), last_interaction_at=None,
                   closed_at=None, source=source, payload=None)
        self.db.connect().execute(
            "INSERT INTO threads(id, subject, state, opened_at, last_interaction_at,"
            " closed_at, source, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, t.subject, t.state, t.opened_at.isoformat(), None, None,
             t.source, None))
        return t

    def get(self, thread_id: str) -> Thread | None:
        row = self.db.connect().execute(
            "SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        return self._row(row) if row is not None else None

    def list_open(self) -> list[Thread]:
        """全部 open（最近活跃在前，无交互记录则按开启时间）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM threads WHERE state = 'open'"
            " ORDER BY COALESCE(last_interaction_at, opened_at) DESC, id DESC").fetchall()
        return [self._row(r) for r in rows]

    def touch(self, thread_id: str, when: datetime) -> bool:
        """记录一次交互（仅限 open 线程）；不存在/已关闭 → False。"""
        cur = self.db.connect().execute(
            "UPDATE threads SET last_interaction_at = ? WHERE id = ? AND state = 'open'",
            (when.isoformat(), thread_id))
        return cur.rowcount > 0

    def close(self, thread_id: str, when: datetime) -> bool:
        """关闭线程；不存在/已关闭 → False（不覆盖 closed_at）。"""
        cur = self.db.connect().execute(
            "UPDATE threads SET state = 'closed', closed_at = ?"
            " WHERE id = ? AND state = 'open'",
            (when.isoformat(), thread_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Thread:
        return Thread(
            id=row["id"], subject=row["subject"], state=row["state"],
            opened_at=datetime.fromisoformat(row["opened_at"]),
            last_interaction_at=(datetime.fromisoformat(row["last_interaction_at"])
                                 if row["last_interaction_at"] is not None else None),
            closed_at=(datetime.fromisoformat(row["closed_at"])
                       if row["closed_at"] is not None else None),
            source=row["source"],
            payload=(json.loads(row["payload"])
                     if row["payload"] is not None else None))
