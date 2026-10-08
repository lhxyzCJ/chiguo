"""storage.repositories.observations — 世界观测（world_observations）仓储。

source 只提供事实（与 events 的 *.observed 对应）；payload 为 JSON（NOT NULL）。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from storage.sqlite.db import Database


@dataclass(frozen=True)
class WorldObservation:
    id: str
    source: str
    type: str
    observed_at: datetime
    expires_at: datetime | None
    event_id: str | None
    payload: dict


class ObservationRepo:
    """world_observations 表：add / recent / active。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, source: str, type: str, observed_at: datetime, payload: dict,
            expires_at: datetime | None = None,
            event_id: str | None = None) -> WorldObservation:
        o = WorldObservation(
            id=uuid.uuid7().hex, source=str(source), type=str(type),
            observed_at=observed_at, expires_at=expires_at, event_id=event_id,
            payload=dict(payload))
        self.db.connect().execute(
            "INSERT INTO world_observations(id, source, type, observed_at,"
            " expires_at, event_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (o.id, o.source, o.type, o.observed_at.isoformat(),
             o.expires_at.isoformat() if o.expires_at is not None else None,
             o.event_id, json.dumps(o.payload, ensure_ascii=False)))
        return o

    def recent(self, source: str | None = None,
               limit: int = 50) -> list[WorldObservation]:
        """最近观测（新→旧），可按 source 过滤。"""
        sql = "SELECT * FROM world_observations"
        args: list = []
        if source is not None:
            sql += " WHERE source = ?"
            args.append(source)
        sql += " ORDER BY observed_at DESC, id DESC LIMIT ?"
        args.append(int(limit))
        rows = self.db.connect().execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    def active(self, now: datetime,
               source: str | None = None) -> list[WorldObservation]:
        """未过期观测（expires_at 为 NULL 视为不过期；expires_at > now），新→旧。"""
        sql = ("SELECT * FROM world_observations"
               " WHERE (expires_at IS NULL OR expires_at > ?)")
        args: list = [now.isoformat()]
        if source is not None:
            sql += " AND source = ?"
            args.append(source)
        sql += " ORDER BY observed_at DESC, id DESC"
        rows = self.db.connect().execute(sql, args).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> WorldObservation:
        return WorldObservation(
            id=row["id"], source=row["source"], type=row["type"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            expires_at=(datetime.fromisoformat(row["expires_at"])
                        if row["expires_at"] is not None else None),
            event_id=row["event_id"], payload=json.loads(row["payload"]))
