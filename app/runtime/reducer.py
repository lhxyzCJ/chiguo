"""app.runtime.reducer — 事件 → 物化状态（Phase 4, #477）。

Reducer = 确定性事件消费者：
- 情绪/关系：按事件重放（时间推进 tick + message.received/sent 结算），
  状态与游标随 runtime_checkpoints 原子落盘（事务内提交 → 恰好一次）；
- 承诺/话题/观测：事件投影到对应表（commitment.created → commitments 行等）。

简化声明（Phase 4 最小版，后续阶段收口）：
- tick 的日程情境参数（is_holiday/in_class/class_load）恒取默认——schedule 感知
  留给 Phase 6 的 source 接入；
- damp（A10 饱和阻尼）恒 1.0——drop_events 窗口统计待 reducer 补齐；
- thread.opened 的 opened_at 用仓储写入时刻（事件时间戳对齐待补充）。
"""
import dataclasses
import random
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from chiguo_time import CST
from domain import affect as affect_mod
from domain import relationship as rel_mod
from storage.events import EventStore
from storage.repositories.checkpoints import CheckpointRepo
from storage.repositories.commitments import CommitmentRepo
from storage.repositories.observations import ObservationRepo
from storage.repositories.threads import ThreadRepo
from storage.sqlite.db import Database

STREAM = "runtime"

# source 观测类事件（投影到 world_observations）
OBSERVATION_TYPES = frozenset({
    "schedule.state", "schedule.course_starting", "holiday.upcoming",
    "anniversary.upcoming", "music.observed", "weather.changed",
})

_NO_USER_SILENT_HOURS = 999.0  # 从未交互（与旧 cooldown.silent_hours 语义一致）


@dataclass(frozen=True)
class RuntimeState:
    affect: object          # AffectState（domain.affect）
    relationship: object    # RelationshipState（domain.relationship）
    last_event_id: str | None
    last_event_at: datetime | None


def in_quiet_hours(now: datetime, quiet_start: int, quiet_end: int) -> bool:
    """静默窗口判定（旧 cooldown.quiet_window 同语义）：qe 不含；相等=不静默；跨午夜。"""
    try:
        qs, qe = int(quiet_start), int(quiet_end)
    except (TypeError, ValueError):
        return False
    h = now.hour
    if qs == qe:
        return False
    if qs < qe:
        return qs <= h < qe
    return h >= qs or h < qe


def _dt(value) -> datetime | None:
    """宽松时间解析：datetime 原样；ISO 字符串（含 date-only）→ CST aware。"""
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CST)
    return dt


def _from_json(cls, d):
    """dataclass 反序列化（字段过滤 + 缺省回退；坏形状 → None）。"""
    if not isinstance(d, dict):
        return None
    kwargs = {}
    for f in dataclasses.fields(cls):
        if f.name in d:
            kwargs[f.name] = d[f.name]
        elif f.default is not dataclasses.MISSING:
            kwargs[f.name] = f.default
        else:
            return None
    try:
        return cls(**kwargs)
    except (TypeError, ValueError):
        return None


class Reducer:
    """事件消费器：catch_up() 增量物化；current() 读当前状态。"""

    def __init__(self, db: Database, config: dict):
        self.db = db
        self.config = config or {}
        self.events = EventStore(db)
        self.checkpoints = CheckpointRepo(db)
        self._st = self._load()

    # ── 加载 / 保存 ───────────────────────────────────────

    def _load(self) -> dict:
        cp = self.checkpoints.get(STREAM)
        saved = cp.state if cp is not None and isinstance(cp.state, dict) else {}
        st = {
            "affect": (_from_json(affect_mod.AffectState, saved.get("affect"))
                       or affect_mod.initial(self.config)),
            "relationship": (_from_json(rel_mod.RelationshipState,
                                        saved.get("relationship"))
                             or rel_mod.initial()),
            "last_user_at": _dt(saved.get("last_user_at")),
            "last_sent_at": _dt(saved.get("last_sent_at")),
            "last_event_id": cp.last_event_id if cp is not None else None,
            "last_event_at": cp.last_occurred_at if cp is not None else None,
        }
        self._rng = self._load_rng(saved.get("rng_state"))
        if cp is not None and saved.get("last_event_at"):
            st["last_event_at"] = _dt(saved["last_event_at"]) or st["last_event_at"]
        return st

    def _load_rng(self, rng_state) -> random.Random:
        try:
            seed = int((self.config.get("emotion", {}) or {}).get("noise_seed", 42))
        except (TypeError, ValueError):
            seed = 42
        rng = random.Random(seed)
        try:
            if isinstance(rng_state, (list, tuple)) and len(rng_state) == 3:
                rng.setstate((int(rng_state[0]),
                              tuple(int(v) for v in rng_state[1]),
                              rng_state[2]))
        except (TypeError, ValueError):
            pass  # 坏 RNG 态 → 重新播种（噪声关闭时无影响）
        return rng

    def _save_checkpoint(self):
        st = self._st
        self.checkpoints.save(
            STREAM,
            last_event_id=st["last_event_id"],
            last_occurred_at=st["last_event_at"],
            state={
                "affect": dataclasses.asdict(st["affect"]),
                "relationship": dataclasses.asdict(st["relationship"]),
                "last_user_at": st["last_user_at"].isoformat() if st["last_user_at"] else None,
                "last_sent_at": st["last_sent_at"].isoformat() if st["last_sent_at"] else None,
                "last_event_at": st["last_event_at"].isoformat() if st["last_event_at"] else None,
                "rng_state": self._rng.getstate(),
            })

    # ── 消费 ─────────────────────────────────────────────

    def catch_up(self, now: datetime | None = None, limit: int = 500) -> int:
        """消费新事件（原子：投影 + 游标同一事务提交）；返回本轮消费数。"""
        batch = self.events.after(self._st["last_event_id"], limit=limit)
        if not batch:
            return 0
        with self.db.transaction():
            for ev in batch:
                self._advance_time(ev.occurred_at)
                self._dispatch(ev)
                self._st["last_event_id"] = ev.event_id
                self._st["last_event_at"] = ev.occurred_at
            self._save_checkpoint()
        return len(batch)

    def current(self) -> RuntimeState:
        return RuntimeState(affect=self._st["affect"],
                            relationship=self._st["relationship"],
                            last_event_id=self._st["last_event_id"],
                            last_event_at=self._st["last_event_at"])

    # ── 内部 ─────────────────────────────────────────────

    def _silent_hours(self, now: datetime) -> float:
        last = self._st["last_user_at"]
        if last is None:
            return _NO_USER_SILENT_HOURS
        return max(0.0, (now - last).total_seconds() / 3600.0)

    def _advance_time(self, at: datetime):
        last = self._st["last_event_at"]
        if last is None or at <= last:
            return
        hours = (at - last).total_seconds() / 3600.0
        if hours <= 0:
            return
        self._st["affect"] = affect_mod.tick(
            self._st["affect"], hours, at,
            silent_hours=self._silent_hours(at), config=self.config,
            rng=self._rng)
        self._st["relationship"] = rel_mod.decay(self._st["relationship"], hours)

    def _dispatch(self, ev):
        handler = getattr(self, "_on_" + ev.type.replace(".", "_"), None)
        if handler is not None:
            handler(ev)
        elif ev.type in OBSERVATION_TYPES:
            self._project_observation(ev)
        # 未知事件类型：只推进游标（前向兼容）

    # 消息

    def _on_message_received(self, ev):
        payload = ev.payload or {}
        analysis = payload.get("analysis")
        if not isinstance(analysis, dict):
            analysis = None
        text = str(payload.get("text") or "")
        last_sent = self._st["last_sent_at"]
        latency_h = ((ev.occurred_at - last_sent).total_seconds() / 3600.0
                     if last_sent is not None else None)
        self._st["affect"] = affect_mod.apply_user_message(
            self._st["affect"], ev.occurred_at, msg_length=len(text),
            latency_hours=latency_h, analysis=analysis, config=self.config)
        warmth = 0.0
        if analysis:
            try:
                warmth = float(analysis.get("warmth", 0.0))
            except (TypeError, ValueError):
                warmth = 0.0
        self._st["relationship"] = rel_mod.apply_event(
            self._st["relationship"], "message.received",
            now=ev.occurred_at, payload={"warmth": warmth})
        self._st["last_user_at"] = ev.occurred_at

    def _on_message_sent(self, ev):
        self._st["affect"] = affect_mod.apply_character_send(
            self._st["affect"], config=self.config)
        self._st["relationship"] = rel_mod.apply_event(
            self._st["relationship"], "message.sent", now=ev.occurred_at,
            payload=dict(ev.payload or {}))
        self._st["last_sent_at"] = ev.occurred_at

    def _on_message_delivery_failed(self, ev):
        self._st["relationship"] = rel_mod.apply_event(
            self._st["relationship"], "message.delivery_failed",
            now=ev.occurred_at, payload=dict(ev.payload or {}))

    _on_message_uncertain = _on_message_delivery_failed

    # 承诺 / 话题

    def _on_commitment_created(self, ev):
        payload = ev.payload or {}
        subject = str(payload.get("subject") or "").strip()
        if not subject:
            return
        CommitmentRepo(self.db).add(
            str(payload.get("kind") or "user_event"), subject,
            due_at=_dt(payload.get("due_at")),
            details=payload.get("details") if isinstance(payload.get("details"), dict) else None,
            created_from_event=ev.event_id)

    def _on_commitment_resolved(self, ev):
        payload = ev.payload or {}
        repo = CommitmentRepo(self.db)
        cid = payload.get("commitment_id")
        target = repo.get(cid) if cid else None
        if target is None and payload.get("subject"):
            subject = str(payload["subject"])
            target = next((c for c in repo.list_open() if c.subject == subject), None)
        if target is not None:
            repo.resolve(target.id, ev.occurred_at, resolution_event=ev.event_id)

    def _on_thread_opened(self, ev):
        payload = ev.payload or {}
        subject = str(payload.get("subject") or "").strip()
        if not subject:
            return
        ThreadRepo(self.db).open_thread(subject, source=payload.get("source"))

    def _on_thread_closed(self, ev):
        payload = ev.payload or {}
        tid = payload.get("thread_id")
        if tid:
            ThreadRepo(self.db).close(tid, ev.occurred_at)

    # 旧链 schedule.created（Phase 3 双写）→ reminder 映射为承诺

    def _on_schedule_created(self, ev):
        payload = ev.payload or {}
        if payload.get("kind") != "reminder":
            return
        item = payload.get("item") or {}
        label = str(item.get("label") or "").strip()
        when = item.get("when") if isinstance(item.get("when"), dict) else {}
        due = _dt(when.get("date") if when else item.get("date"))
        if not label:
            return
        if due is not None and due.hour == 0 and due.minute == 0:
            due = due.replace(hour=9, minute=0)  # date-only → 当日 09:00
        CommitmentRepo(self.db).add("reminder", label, due_at=due,
                                    created_from_event=ev.event_id)

    # 观测投影

    def _project_observation(self, ev):
        payload = ev.payload or {}
        observed_at = _dt(payload.get("observed_at")) or ev.occurred_at
        data = payload.get("data")
        if not isinstance(data, dict):
            data = {k: v for k, v in payload.items()
                    if k not in ("observed_at", "expires_at")}
        ObservationRepo(self.db).add(
            source=ev.source, type=ev.type, observed_at=observed_at,
            payload=data, expires_at=_dt(payload.get("expires_at")),
            event_id=ev.event_id)
