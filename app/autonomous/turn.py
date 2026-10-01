"""app.autonomous.turn — 自主回合（v2 主循环，Phase 5, #477）。

一次 autonomous_turn 的完整链路：
  提取（message → commitment 等结构化事件）
  → 归约（reducer：事件 → 物化状态）
  → 观测（sources.observe → *.observed 事件 → world_observations）
  → 机会发现（opportunity drafts）
  → 驱动力评估（drives）
  → 约束（静默窗口）→ planner（intent / wait / defer）
  → 落库（opportunities / drives / intent / turn 审计行）

shadow 语义（默认）：只记录决策，不发送。`execute=True` 时仅创建
status=pending 的 send_message action 行（真正送达在 Phase 6 的 executor）。
"""
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta

from chiguo_paths import PROJECT_ROOT
from chiguo_time import CST
from domain.planning.drives import evaluate_drives
from domain.planning.opportunities import discover_opportunities
from domain.planning.planner import Defer, IntentDraft, Wait, plan
from app.runtime.extractor import Extractor
from app.runtime.reducer import STREAM as REDUCER_STREAM, in_quiet_hours
from app.runtime.reducer import Reducer
from sources.base import observe_all
from storage.events import EventStore
from storage.repositories.actions import ActionRepo
from storage.repositories.checkpoints import CheckpointRepo
from storage.repositories.commitments import CommitmentRepo
from storage.repositories.drives import DriveRepo, IntentRepo
from storage.repositories.observations import ObservationRepo
from storage.repositories.opportunities import OpportunityRepo
from storage.repositories.threads import ThreadRepo
from storage.repositories.turns import TurnRepo
from storage.sqlite.db import Database


@dataclass(frozen=True)
class TurnResult:
    turn_id: str
    reason: str
    outcome: str            # intent / waited / deferred / action_pending
    intent_id: str | None
    action_id: str | None
    why: dict


def build_sources(config: dict, base_dir: str) -> list:
    """构造内置观测源（单源构造失败 → 跳过该源，不影响其余）。"""
    src: list = []
    try:
        from sources.schedule import ScheduleSource
        src.append(ScheduleSource(base_dir, config))
    except Exception as e:  # noqa: BLE001
        print(f"[turn] ScheduleSource 构造失败，跳过: {e}", file=sys.stderr)
    try:
        from sources.holiday import HolidaySource
        src.append(HolidaySource(base_dir))
    except Exception as e:  # noqa: BLE001
        print(f"[turn] HolidaySource 构造失败，跳过: {e}", file=sys.stderr)
    try:
        from sources.netease import NeteaseSource
        src.append(NeteaseSource(base_dir=base_dir, config=config))
    except Exception as e:  # noqa: BLE001
        print(f"[turn] NeteaseSource 构造失败，跳过: {e}", file=sys.stderr)
    try:
        from sources.weather import WeatherSource
        src.append(WeatherSource(config))
    except Exception as e:  # noqa: BLE001
        print(f"[turn] WeatherSource 构造失败，跳过: {e}", file=sys.stderr)
    return src


def _next_quiet_end(now: datetime, quiet_start: int, quiet_end: int) -> datetime | None:
    """当前（或下一次）静默窗口的结束时刻；不在静默窗内 → None。"""
    if not in_quiet_hours(now, quiet_start, quiet_end):
        return None
    try:
        qe = int(quiet_end)
    except (TypeError, ValueError):
        return None
    candidate = datetime.combine(now.date(), dtime(hour=qe), tzinfo=CST)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate


def autonomous_turn(*, db: Database, config: dict, reason: str = "manual",
                    now: datetime | None = None, base_dir: str | None = None,
                    execute: bool = False) -> TurnResult:
    now = now or datetime.now(CST)
    started_at = datetime.now(CST)
    base_dir = base_dir or config.get("_base_dir") or str(PROJECT_ROOT)
    store = EventStore(db)

    prev_cp = CheckpointRepo(db).get(REDUCER_STREAM)
    prev_event_id = prev_cp.last_event_id if prev_cp is not None else None

    # ① 提取（确定性规则；LLM 提取留待 Phase 7）
    try:
        Extractor(db).catch_up(now)
    except Exception as e:  # noqa: BLE001 —— 提取失败不阻断回合
        print(f"[turn] extractor 失败（跳过）: {e}", file=sys.stderr)

    # ② 状态归约
    reducer = Reducer(db, config)
    reducer.catch_up(now)

    # ③ 世界观测（写入事件，由 reducer 投影）
    observations = observe_all(build_sources(config, base_dir), now)
    for ob in observations:
        store.append(ob.type, source=ob.source, occurred_at=ob.observed_at,
                     payload={"observed_at": ob.observed_at.isoformat(),
                              "expires_at": (ob.expires_at.isoformat()
                                             if ob.expires_at is not None else None),
                              "data": ob.payload})
    if observations:
        reducer.catch_up(now)
    state = reducer.current()

    # ④ 机会发现
    open_commitments = CommitmentRepo(db).list_open()
    open_threads = ThreadRepo(db).list_open()
    world_obs = ObservationRepo(db).active(now)
    opps = discover_opportunities(observations=world_obs,
                                  commitments=open_commitments,
                                  threads=open_threads, now=now,
                                  config=config.get("planning") or {})

    # ⑤ 驱动力
    drives = evaluate_drives(affect=state.affect, relationship=state.relationship,
                             opportunities=opps, now=now)

    # ⑥ 约束（静默窗口）→ ⑦ planner
    sched_cfg = config.get("schedule") or {}
    qs = sched_cfg.get("quiet_start", 0)
    qe = sched_cfg.get("quiet_end", 8)
    quiet = in_quiet_hours(now, qs, qe)
    decision = plan(opportunities=opps, drives=drives, now=now,
                    constraints={"quiet": quiet,
                                 "quiet_until": _next_quiet_end(now, qs, qe)})

    # ⑧ 落库
    turn_id = uuid.uuid7().hex
    opp_ids = []
    opp_repo = OpportunityRepo(db)
    for d in opps:
        row = opp_repo.add(d.kind, expires_at=d.expires_at,
                           observation_event_id=d.observation_event_id,
                           novelty=d.novelty, relevance=d.relevance,
                           urgency=d.urgency,
                           emotional_affordance=d.emotional_affordance,
                           payload=dict(d.payload))
        opp_ids.append(row.id)
    drive_ids = []
    drive_repo = DriveRepo(db)
    for d in drives:
        row = drive_repo.add(d.kind, d.intensity, inputs=dict(d.inputs or {}),
                             autonomous_turn_id=turn_id)
        drive_ids.append(row.id)

    intent_id = None
    action_id = None
    why: dict = {}
    if isinstance(decision, IntentDraft):
        outcome = "intent"
        why = dict(decision.why)
        intent = IntentRepo(db).add(decision.type, why=decision.why,
                                    plan=decision.plan, autonomous_turn_id=turn_id)
        intent_id = intent.id
        if execute:
            action = ActionRepo(db).add("send_message", intent_id=intent_id,
                                        correlation_id=turn_id,
                                        input=dict(decision.plan))
            action_id = action.id
            outcome = "action_pending"
    elif isinstance(decision, Defer):
        outcome = "deferred"
        why = {"reason": decision.reason, "until": decision.until.isoformat()
               if decision.until else None,
               "candidate": decision.candidate.type if decision.candidate else None}
    else:
        outcome = "waited"
        why = {"reason": decision.reason}

    snapshot = {
        "affect": {"loneliness": round(state.affect.loneliness, 2),
                   "affection": round(state.affect.affection, 2),
                   "anxiety": round(state.affect.anxiety, 2),
                   "energy": round(state.affect.energy, 2),
                   "dominant_layer": state.affect.dominant_layer},
        "relationship": {"closeness": round(state.relationship.closeness, 3),
                         "initiative_balance": round(state.relationship.initiative_balance, 3),
                         "recent_warmth": round(state.relationship.recent_warmth, 3)},
        "quiet": quiet,
        "opportunities": [d.kind for d in opps],
        "drives": {d.kind: round(d.intensity, 3) for d in drives},
        "planner": outcome,
    }
    TurnRepo(db).add(reason, started_at=started_at,
                     finished_at=datetime.now(CST),
                     event_window_from=prev_event_id,
                     event_window_to=state.last_event_id,
                     state_snapshot=snapshot, opportunity_ids=opp_ids,
                     drive_ids=drive_ids, intent_id=intent_id,
                     action_id=action_id, outcome=outcome)
    return TurnResult(turn_id=turn_id, reason=reason, outcome=outcome,
                      intent_id=intent_id, action_id=action_id, why=why)
