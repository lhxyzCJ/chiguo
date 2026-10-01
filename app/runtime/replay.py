"""app.runtime.replay — 历史重放（Phase 9, #477）。

把生产库在线备份到临时副本，在副本上从零消费事件（绕过既有游标），
对窗口内的每个 wake 事件重跑：归约 → 机会发现 → 驱动力 → planner，
返回逐步决策（含 why）。**不发送、不触碰生产库**。

与真实回合的差异（有意）：
- 不跑 sources（不重新观测世界）——只用事件流里已存在的观测；
- 不跑 extractor（不重复产生承诺）——只消费既有事件；
- 不落 opportunities/drives/intent/turn 行（纯内存结果）。
"""
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.runtime.reducer import Reducer, in_quiet_hours, next_quiet_end
from domain.planning.drives import evaluate_drives
from domain.planning.opportunities import discover_opportunities
from domain.planning.planner import Defer, IntentDraft, plan
from storage.events import EventStore
from storage.repositories.commitments import CommitmentRepo
from storage.repositories.observations import ObservationRepo
from storage.repositories.threads import ThreadRepo
from storage.sqlite.db import Database


@dataclass(frozen=True)
class ReplayDecision:
    event_id: str
    at: datetime
    outcome: str                # intent / waited / deferred
    intent_type: str | None
    why: dict
    opportunities: tuple = ()   # 本轮机会 kinds（诊断：重复投影会在此显形）


def replay(db: Database, config: dict, *, since: datetime,
           until: datetime | None = None, limit: int = 200) -> list[ReplayDecision]:
    """在临时副本上重放 [since, until) 内的 wake 决策；返回决策列表。"""
    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "replay.sqlite"
        db.backup(work)
        wdb = Database(work)
        # 重放语义：从零消费（清游标）+ 清空事件派生投影表——否则副本里已物化的
        # 承诺/话题/观测会被再次投影（重复事实 → 机会重复/张力翻倍）。
        # 删除顺序：先子后父（外键 ON）。
        with wdb.transaction() as conn:
            conn.execute("DELETE FROM runtime_checkpoints")
            for table in ("deliveries", "actions", "intents", "drives",
                          "opportunities", "world_observations", "threads",
                          "commitments", "autonomous_turns"):
                conn.execute(f"DELETE FROM {table}")
        reducer = Reducer(wdb, config)
        store = EventStore(wdb)

        results: list[ReplayDecision] = []
        for ev in store.after(None, limit=1_000_000):
            reducer.catch_up(limit=1)  # 与迭代同序（event_id 升序），逐条推进状态
            if ev.type != "wake":
                continue
            if ev.occurred_at < since or (until is not None and ev.occurred_at >= until):
                continue
            state = reducer.current()
            opps = discover_opportunities(
                observations=ObservationRepo(wdb).active(ev.occurred_at),
                commitments=CommitmentRepo(wdb).list_open(),
                threads=ThreadRepo(wdb).list_open(),
                now=ev.occurred_at, config=config.get("planning") or {})
            drives = evaluate_drives(affect=state.affect,
                                     relationship=state.relationship,
                                     opportunities=opps, now=ev.occurred_at)
            sched = config.get("schedule") or {}
            qs, qe = sched.get("quiet_start", 0), sched.get("quiet_end", 8)
            decision = plan(opportunities=opps, drives=drives, now=ev.occurred_at,
                            constraints={
                                "quiet": in_quiet_hours(ev.occurred_at, qs, qe),
                                "quiet_until": next_quiet_end(ev.occurred_at, qs, qe)})
            opp_kinds = tuple(d.kind for d in opps)
            if isinstance(decision, IntentDraft):
                results.append(ReplayDecision(
                    event_id=ev.event_id, at=ev.occurred_at, outcome="intent",
                    intent_type=decision.type, why=dict(decision.why),
                    opportunities=opp_kinds))
            elif isinstance(decision, Defer):
                results.append(ReplayDecision(
                    event_id=ev.event_id, at=ev.occurred_at, outcome="deferred",
                    intent_type=(decision.candidate.type
                                 if decision.candidate else None),
                    why={"reason": decision.reason}, opportunities=opp_kinds))
            else:
                results.append(ReplayDecision(
                    event_id=ev.event_id, at=ev.occurred_at, outcome="waited",
                    intent_type=None, why={"reason": decision.reason},
                    opportunities=opp_kinds))
            if len(results) >= limit:
                break
        return results
