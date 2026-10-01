"""storage.repositories.checkpoints — 运行时消费游标（runtime_checkpoints）。

reducer / extractor / scheduler 等增量消费者各自持一条 stream：
记「已消费到哪个 event_id」+ 可选 JSON 状态（如 affect/relationship 快照、
RNG 态），重启后从游标继续，无需全量重放。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Checkpoint:
    stream: str
    last_event_id: str | None
    last_occurred_at: datetime | None
    state: dict
    updated_at: datetime


class CheckpointRepo:
    """runtime_checkpoints 表：get / save（upsert）。"""

    def __init__(self, db: Database):
        self.db = db

    def get(self, stream: str) -> Checkpoint | None:
        row = self.db.connect().execute(
            "SELECT * FROM runtime_checkpoints WHERE stream = ?",
            (str(stream),)).fetchone()
        return self._row(row) if row is not None else None

    def save(self, stream: str, *, last_event_id: str | None,
             last_occurred_at: datetime | None, state: dict | None = None):
        """写入或覆盖该 stream 的游标（upsert）。"""
        self.db.connect().execute(
            "INSERT INTO runtime_checkpoints(stream, last_event_id,"
            " last_occurred_at, state, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(stream) DO UPDATE SET"
            " last_event_id = excluded.last_event_id,"
            " last_occurred_at = excluded.last_occurred_at,"
            " state = excluded.state, updated_at = excluded.updated_at",
            (str(stream), last_event_id,
             last_occurred_at.isoformat() if last_occurred_at is not None else None,
             json.dumps(state or {}, ensure_ascii=False),
             datetime.now(CST).isoformat()))

    @staticmethod
    def _row(row) -> Checkpoint:
        return Checkpoint(
            stream=row["stream"],
            last_event_id=row["last_event_id"],
            last_occurred_at=(datetime.fromisoformat(row["last_occurred_at"])
                              if row["last_occurred_at"] is not None else None),
            state=(json.loads(row["state"]) if row["state"] is not None else {}),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )
