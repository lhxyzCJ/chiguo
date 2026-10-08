"""storage.repositories.messages — sessions / messages 仓储。

sessions 是 Pi transcript 的语义镜像（pi_session_id 关联 Pi 会话文件）；
messages.analysis 为 JSON（情绪分析），仓储边界做 dict ⇄ TEXT 转换。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Session:
    id: str
    kind: str
    pi_session_id: str | None
    started_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True)
class Message:
    id: str
    session_id: str | None
    direction: str
    text: str
    at: datetime
    event_id: str | None
    analysis: dict | None
    delivery_id: str | None


class SessionRepo:
    """sessions 表：add / get / end。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, kind: str, pi_session_id: str | None = None) -> Session:
        s = Session(id=uuid.uuid7().hex, kind=str(kind), pi_session_id=pi_session_id,
                    started_at=datetime.now(CST), ended_at=None)
        self.db.connect().execute(
            "INSERT INTO sessions(id, kind, pi_session_id, started_at, ended_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (s.id, s.kind, s.pi_session_id, s.started_at.isoformat(), None))
        return s

    def get(self, session_id: str) -> Session | None:
        row = self.db.connect().execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._row(row) if row is not None else None

    def end(self, session_id: str, when: datetime | None = None) -> bool:
        """标记会话结束；已结束或不存在 → False。"""
        cur = self.db.connect().execute(
            "UPDATE sessions SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
            ((when or datetime.now(CST)).isoformat(), session_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Session:
        return Session(
            id=row["id"], kind=row["kind"], pi_session_id=row["pi_session_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            ended_at=(datetime.fromisoformat(row["ended_at"])
                      if row["ended_at"] is not None else None))


class MessageRepo:
    """messages 表：add / get / recent / for_session。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, direction: str, text: str, session_id: str | None = None,
            at: datetime | None = None, event_id: str | None = None,
            analysis: dict | None = None,
            delivery_id: str | None = None) -> Message:
        m = Message(
            id=uuid.uuid7().hex, session_id=session_id, direction=str(direction),
            text=str(text), at=at or datetime.now(CST), event_id=event_id,
            analysis=dict(analysis) if analysis is not None else None,
            delivery_id=delivery_id)
        self.db.connect().execute(
            "INSERT INTO messages(id, session_id, direction, text, at, event_id,"
            " analysis, delivery_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.session_id, m.direction, m.text, m.at.isoformat(), m.event_id,
             (json.dumps(m.analysis, ensure_ascii=False)
              if m.analysis is not None else None), m.delivery_id))
        return m

    def get(self, message_id: str) -> Message | None:
        row = self.db.connect().execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return self._row(row) if row is not None else None

    def recent(self, limit: int = 50) -> list[Message]:
        """最近消息（新→旧）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM messages ORDER BY at DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    def for_session(self, session_id: str) -> list[Message]:
        """某会话的全部消息（旧→新）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY at ASC, id ASC",
            (session_id,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Message:
        return Message(
            id=row["id"], session_id=row["session_id"], direction=row["direction"],
            text=row["text"], at=datetime.fromisoformat(row["at"]),
            event_id=row["event_id"],
            analysis=(json.loads(row["analysis"])
                      if row["analysis"] is not None else None),
            delivery_id=row["delivery_id"])
