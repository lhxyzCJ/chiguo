"""storage.repositories.drives — 驱动力（drives）与意图（intents）仓储。

`inputs`（drive）与 `why`/`plan`（intent）为 JSON，仓储边界做 dict ⇄ TEXT 转换。
"""
import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Drive:
    id: str
    kind: str
    intensity: float
    evaluated_at: datetime
    status: str
    inputs: dict | None
    autonomous_turn_id: str | None


@dataclass(frozen=True)
class Intent:
    id: str
    type: str
    why: dict
    plan: dict | None
    created_at: datetime
    status: str
    autonomous_turn_id: str | None


class DriveRepo:
    """drives 表：add / for_turn。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, kind: str, intensity: float, inputs: dict | None = None,
            autonomous_turn_id: str | None = None) -> Drive:
        d = Drive(id=uuid.uuid7().hex, kind=str(kind), intensity=float(intensity),
                  evaluated_at=datetime.now(CST), status="active",
                  inputs=dict(inputs) if inputs is not None else None,
                  autonomous_turn_id=autonomous_turn_id)
        self.db.connect().execute(
            "INSERT INTO drives(id, kind, intensity, evaluated_at, status, inputs,"
            " autonomous_turn_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (d.id, d.kind, d.intensity, d.evaluated_at.isoformat(), d.status,
             (json.dumps(d.inputs, ensure_ascii=False)
              if d.inputs is not None else None), d.autonomous_turn_id))
        return d

    def for_turn(self, turn_id: str) -> list[Drive]:
        """某次自主回合评估出的驱动力（评估时间升序）。"""
        rows = self.db.connect().execute(
            "SELECT * FROM drives WHERE autonomous_turn_id = ?"
            " ORDER BY evaluated_at ASC, id ASC", (turn_id,)).fetchall()
        return [self._row(r) for r in rows]

    @staticmethod
    def _row(row) -> Drive:
        return Drive(
            id=row["id"], kind=row["kind"], intensity=row["intensity"],
            evaluated_at=datetime.fromisoformat(row["evaluated_at"]),
            status=row["status"],
            inputs=(json.loads(row["inputs"])
                    if row["inputs"] is not None else None),
            autonomous_turn_id=row["autonomous_turn_id"])


class IntentRepo:
    """intents 表：add / get / set_status。"""

    def __init__(self, db: Database):
        self.db = db

    def add(self, type: str, why: dict, plan: dict | None = None,
            autonomous_turn_id: str | None = None) -> Intent:
        it = Intent(id=uuid.uuid7().hex, type=str(type), why=dict(why),
                    plan=dict(plan) if plan is not None else None,
                    created_at=datetime.now(CST), status="open",
                    autonomous_turn_id=autonomous_turn_id)
        self.db.connect().execute(
            "INSERT INTO intents(id, type, why, plan, created_at, status,"
            " autonomous_turn_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (it.id, it.type, json.dumps(it.why, ensure_ascii=False),
             (json.dumps(it.plan, ensure_ascii=False)
              if it.plan is not None else None),
             it.created_at.isoformat(), it.status, it.autonomous_turn_id))
        return it

    def get(self, intent_id: str) -> Intent | None:
        row = self.db.connect().execute(
            "SELECT * FROM intents WHERE id = ?", (intent_id,)).fetchone()
        return self._row(row) if row is not None else None

    def set_status(self, intent_id: str, status: str) -> bool:
        """状态迁移（open/done/abandoned）；不存在 → False。"""
        cur = self.db.connect().execute(
            "UPDATE intents SET status = ? WHERE id = ?", (str(status), intent_id))
        return cur.rowcount > 0

    @staticmethod
    def _row(row) -> Intent:
        return Intent(
            id=row["id"], type=row["type"], why=json.loads(row["why"]),
            plan=(json.loads(row["plan"]) if row["plan"] is not None else None),
            created_at=datetime.fromisoformat(row["created_at"]),
            status=row["status"], autonomous_turn_id=row["autonomous_turn_id"])
