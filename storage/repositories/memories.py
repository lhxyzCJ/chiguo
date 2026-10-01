"""storage.repositories.memories — 长期记忆（canonical）与链接仓储。

SQLite memories 为事实来源（mem0 仅语义索引）；同一事实更新 = 新记录 +
旧记录置 superseded（保留历史，不覆盖不删除），并在 memory_links 留
relation='supersedes' 的边（from=新记忆, to=旧记忆）。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Memory:
    id: str
    kind: str
    text: str
    source: str | None
    origin: str | None
    confidence: float | None
    observed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    valid_from: datetime | None
    valid_to: datetime | None
    status: str
    importance: float | None
    emotion_tag: str | None
    mem0_id: str | None
    meta: dict | None


class MemoryRepo:
    """memories / memory_links 表：add / get / list_active / supersede / set_status。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, kind: str, text: str, source: str | None = None,
            origin: str | None = None, confidence: float | None = None,
            observed_at: datetime | None = None,
            valid_from: datetime | None = None,
            valid_to: datetime | None = None, importance: float | None = None,
            emotion_tag: str | None = None, mem0_id: str | None = None,
            meta: dict | None = None) -> Memory:
        now = datetime.now(CST)
        m = Memory(
            id=uuid.uuid7().hex, kind=str(kind), text=str(text), source=source,
            origin=origin, confidence=confidence, observed_at=observed_at,
            created_at=now, updated_at=now, valid_from=valid_from,
            valid_to=valid_to, status="active", importance=importance,
            emotion_tag=emotion_tag, mem0_id=mem0_id,
            meta=dict(meta) if meta is not None else None)
        self.db.connect().execute(
            "INSERT INTO memories(id, kind, text, source, origin, confidence,"
            " observed_at, created_at, updated_at, valid_from, valid_to, status,"
            " importance, emotion_tag, mem0_id, meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (m.id, m.kind, m.text, m.source, m.origin, m.confidence,
             m.observed_at.isoformat() if m.observed_at is not None else None,
             m.created_at.isoformat(), m.updated_at.isoformat(),
             m.valid_from.isoformat() if m.valid_from is not None else None,
             m.valid_to.isoformat() if m.valid_to is not None else None,
             m.status, m.importance, m.emotion_tag, m.mem0_id,
             (json.dumps(m.meta, ensure_ascii=False)
              if m.meta is not None else None)))
        return m

    def get(self, memory_id: str) -> Memory | None:
        row = self.db.connect().execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return self._row(row) if row is not None else None

    def list_active(self, kind: str | None = None) -> list[Memory]:
        """status=active 的记忆（创建时间升序，重放顺序确定），可按 kind 过滤。"""
        sql = "SELECT * FROM memories WHERE status = 'active'"
        args: list = []
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY created_at ASC, id ASC"
        rows = self.db.connect().execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def supersede(self, old_id: str, new_id: str, when: datetime) -> bool:
        """旧记忆被新记忆取代：旧行 status=superseded + valid_to=when，插链接。

        旧行不存在或非 active → False（不插链接）；新 id 违反外键 → 事务整体
        回滚（旧行保持原状）并自然抛出 sqlite3.IntegrityError。
        """
        with self.db.transaction() as conn:
            cur = conn.execute(
                "UPDATE memories SET status = 'superseded', valid_to = ?,"
                " updated_at = ? WHERE id = ? AND status = 'active'",
                (when.isoformat(), when.isoformat(), old_id))
            if cur.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO memory_links(from_memory_id, to_memory_id, relation,"
                " created_at) VALUES (?, ?, ?, ?)",
                (new_id, old_id, "supersedes", when.isoformat()))
        return True

    def set_status(self, memory_id: str, status: str) -> bool:
        """状态迁移（active/stale/superseded/forgotten/conflicted）；不存在 → False。"""
        cur = self.db.connect().execute(
            "UPDATE memories SET status = ?, updated_at = ? WHERE id = ?",
            (str(status), datetime.now(CST).isoformat(), memory_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Memory:
        return Memory(
            id=row["id"], kind=row["kind"], text=row["text"], source=row["source"],
            origin=row["origin"], confidence=row["confidence"],
            observed_at=(datetime.fromisoformat(row["observed_at"])
                         if row["observed_at"] is not None else None),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            valid_from=(datetime.fromisoformat(row["valid_from"])
                        if row["valid_from"] is not None else None),
            valid_to=(datetime.fromisoformat(row["valid_to"])
                      if row["valid_to"] is not None else None),
            status=row["status"], importance=row["importance"],
            emotion_tag=row["emotion_tag"], mem0_id=row["mem0_id"],
            meta=(json.loads(row["meta"]) if row["meta"] is not None else None))
