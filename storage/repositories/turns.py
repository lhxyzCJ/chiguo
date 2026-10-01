"""storage.repositories.turns — 自主回合审计（autonomous_turns 表）。

每次 autonomous_turn 一行：消费的事件窗口、状态快照、产出的
opportunity/drive/intent/action 指针与结果——replay/shadow 与
「为什么」排查的骨架记录。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Turn:
    id: str
    reason: str
    started_at: datetime
    finished_at: datetime | None
    event_window_from: str | None
    event_window_to: str | None
    state_snapshot: dict | None
    opportunity_ids: list | None
    drive_ids: list | None
    intent_id: str | None
    action_id: str | None
    outcome: str | None


class TurnRepo:
    """autonomous_turns 表：add / get / recent。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, reason: str, *, started_at: datetime | None = None,
            finished_at: datetime | None = None,
            event_window_from: str | None = None,
            event_window_to: str | None = None,
            state_snapshot: dict | None = None,
            opportunity_ids: list | None = None,
            drive_ids: list | None = None,
            intent_id: str | None = None,
            action_id: str | None = None,
            outcome: str | None = None) -> Turn:
        now = datetime.now(CST)
        t = Turn(id=uuid.uuid7().hex, reason=str(reason),
                 started_at=started_at or now, finished_at=finished_at,
                 event_window_from=event_window_from, event_window_to=event_window_to,
                 state_snapshot=state_snapshot, opportunity_ids=opportunity_ids,
                 drive_ids=drive_ids, intent_id=intent_id, action_id=action_id,
                 outcome=outcome)
        self.db.connect().execute(
            "INSERT INTO autonomous_turns(id, reason, started_at, finished_at,"
            " event_window_from, event_window_to, state_snapshot, opportunity_ids,"
            " drive_ids, intent_id, action_id, outcome)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (t.id, t.reason, t.started_at.isoformat(),
             t.finished_at.isoformat() if t.finished_at else None,
             t.event_window_from, t.event_window_to,
             json.dumps(t.state_snapshot, ensure_ascii=False) if t.state_snapshot is not None else None,
             json.dumps(t.opportunity_ids, ensure_ascii=False) if t.opportunity_ids is not None else None,
             json.dumps(t.drive_ids, ensure_ascii=False) if t.drive_ids is not None else None,
             t.intent_id, t.action_id, t.outcome))
        return t

    def get(self, turn_id: str) -> Turn | None:
        row = self.db.connect().execute(
            "SELECT * FROM autonomous_turns WHERE id = ?", (turn_id,)).fetchone()
        return self._row(row) if row is not None else None

    def recent(self, limit: int = 20) -> list[Turn]:
        rows = self.db.connect().execute(
            "SELECT * FROM autonomous_turns ORDER BY started_at DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Turn:
        return Turn(
            id=row["id"], reason=row["reason"],
            started_at=datetime.fromisoformat(row["started_at"]),
            finished_at=(datetime.fromisoformat(row["finished_at"])
                         if row["finished_at"] is not None else None),
            event_window_from=row["event_window_from"],
            event_window_to=row["event_window_to"],
            state_snapshot=(json.loads(row["state_snapshot"])
                            if row["state_snapshot"] is not None else None),
            opportunity_ids=(json.loads(row["opportunity_ids"])
                             if row["opportunity_ids"] is not None else None),
            drive_ids=(json.loads(row["drive_ids"])
                       if row["drive_ids"] is not None else None),
            intent_id=row["intent_id"], action_id=row["action_id"],
            outcome=row["outcome"])
