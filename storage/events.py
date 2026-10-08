"""storage.events — 事件日志（append-only）+ 因果链查询。

事件是 Chiguo v2 的因果链载体：所有重要外部输入与 agent 行为都落为事件；
`correlation_id` 关联同一 interaction / thread / workflow，`causation_id` 指向
直接上游事件。「为什么发了这条消息」= 从 `message.sent` 沿 causation 走到根。

增量消费游标 = **rowid（提交序）**：rowid 在写锁内分配，严格等于事务提交顺序；
并发进程/线程下 uuid7 的生成顺序可能乱序提交，按 uuid 字典序做游标会永久漏事件。
"""
import dataclasses
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from chiguo_time import CST
from storage.sqlite.db import Database


@dataclass(frozen=True)
class Event:
    event_id: str
    type: str
    occurred_at: datetime
    observed_at: datetime
    source: str
    correlation_id: str | None
    causation_id: str | None
    payload: dict
    # rowid = 提交序游标；只用于增量消费，不参与相等性比较（compare=False）
    cursor: int | None = field(default=None, compare=False)


_CHAIN_GUARD = 1000  # 因果链防环/防脏数据上限


class EventStore:
    """events 表的读写门面（append 为单条 INSERT 原子写）。"""

    def __init__(self, db: Database):
        self.db = db

    def append(self, type: str, *, source: str, payload: dict | None = None,
               occurred_at: datetime | None = None,
               observed_at: datetime | None = None,
               correlation_id: str | None = None,
               causation_id: str | None = None) -> Event:
        now = datetime.now(CST)
        ev = Event(
            event_id=uuid.uuid7().hex,
            type=str(type),
            occurred_at=occurred_at or now,
            observed_at=observed_at or now,
            source=str(source),
            correlation_id=correlation_id,
            causation_id=causation_id,
            payload=dict(payload or {}),
        )
        conn = self.db.connect()
        cur = conn.execute(
            "INSERT INTO events(event_id, type, occurred_at, observed_at, source,"
            " correlation_id, causation_id, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ev.event_id, ev.type, ev.occurred_at.isoformat(), ev.observed_at.isoformat(),
             ev.source, ev.correlation_id, ev.causation_id,
             json.dumps(ev.payload, ensure_ascii=False)))
        return dataclasses.replace(ev, cursor=int(cur.lastrowid))

    def get(self, event_id: str) -> Event | None:
        conn = self.db.connect()
        row = conn.execute(
            "SELECT *, rowid AS _rowid FROM events WHERE event_id = ?",
            (event_id,)).fetchone()
        return self._row(row) if row is not None else None

    def recent(self, *, limit: int = 50, type: str | None = None,
               source: str | None = None) -> list[Event]:
        """最近事件（新→旧）。"""
        sql = "SELECT *, rowid AS _rowid FROM events"
        where, args = [], []
        if type is not None:
            where.append("type = ?")
            args.append(type)
        if source is not None:
            where.append("source = ?")
            args.append(source)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        args.append(int(limit))
        conn = self.db.connect()
        return [self._row(r) for r in conn.execute(sql, args)]

    def since(self, since: datetime, until: datetime | None = None, *,
              type: str | None = None) -> list[Event]:
        """时间窗内事件（旧→新，供 replay/重放）。"""
        sql = "SELECT *, rowid AS _rowid FROM events WHERE occurred_at >= ?"
        args = [since.isoformat()]
        if until is not None:
            sql += " AND occurred_at < ?"
            args.append(until.isoformat())
        if type is not None:
            sql += " AND type = ?"
            args.append(type)
        sql += " ORDER BY occurred_at ASC, event_id ASC"
        conn = self.db.connect()
        return [self._row(r) for r in conn.execute(sql, args)]

    def chain(self, event_id: str) -> list[Event]:
        """沿 causation_id 回溯的完整因果链（根在前、event_id 在后）。

        未知 id → 空列表；链中缺失上游/成环 → 尽力而为截断（不抛）。
        """
        conn = self.db.connect()
        out: list[Event] = []
        seen: set[str] = set()
        cur_id: str | None = event_id
        while cur_id and cur_id not in seen and len(out) < _CHAIN_GUARD:
            seen.add(cur_id)
            row = conn.execute(
                "SELECT *, rowid AS _rowid FROM events WHERE event_id = ?",
                (cur_id,)).fetchone()
            if row is None:
                break
            ev = self._row(row)
            out.append(ev)
            cur_id = ev.causation_id
        out.reverse()
        return out

    def after(self, cursor, *, limit: int = 200) -> list[Event]:
        """游标读取：cursor（rowid）之后的事件，按 rowid 升序，含 limit。

        cursor 为 None/空 → 从头读；非法值按 0 处理。
        """
        try:
            cur = int(cursor) if cursor not in (None, "") else 0
        except (TypeError, ValueError):
            cur = 0
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT *, rowid AS _rowid FROM events WHERE rowid > ?"
            " ORDER BY rowid ASC LIMIT ?", (cur, int(limit))).fetchall()
        return [self._row(r) for r in rows]

    def latest_cursor(self) -> int:
        """当前最大 rowid（0 = 无事件）。"""
        conn = self.db.connect()
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) AS m FROM events").fetchone()
        return int(row["m"])

    def caused_by(self, event_id: str, *, limit: int = 50) -> list[Event]:
        """直接子事件（causation_id == event_id），按发生时间旧→新。"""
        conn = self.db.connect()
        rows = conn.execute(
            "SELECT *, rowid AS _rowid FROM events WHERE causation_id = ?"
            " ORDER BY occurred_at ASC, event_id ASC LIMIT ?",
            (event_id, int(limit))).fetchall()
        return [self._row(r) for r in rows]

    def count(self) -> int:
        conn = self.db.connect()
        return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    @staticmethod
    def _row(row) -> Event:
        return Event(
            event_id=row["event_id"],
            type=row["type"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            observed_at=datetime.fromisoformat(row["observed_at"]),
            source=row["source"],
            correlation_id=row["correlation_id"],
            causation_id=row["causation_id"],
            payload=json.loads(row["payload"] or "{}"),
            cursor=(row["_rowid"] if "_rowid" in row.keys() else None),
        )
