"""domain.planning.planner — 意图规划（机会 + 驱动力 + 约束 → Intent / Wait / Defer）。

纯函数：不发送、不落库，只给出「现在该做什么」的建议与完整理由（why），
执行与硬门控由 runtime/actions 层负责（planner 不做安全拦截）。

规则（最小确定性版）：
- 只有分数 ≥ MIN_ELIGIBLE_SCORE 的机会可作 primary（天气单独不触发——
  它是小线索，只能作 secondary）；其余机会进 secondary 作上下文。
- 约束 quiet（静默窗）→ Defer(until=quiet_until, candidate=本次本会做的意图)。
- 无合格机会 → Wait("no_eligible_opportunity")。
"""
from dataclasses import dataclass
from datetime import datetime

MIN_ELIGIBLE_SCORE = 0.35

# 机会 kind → 意图类型
KIND_TO_INTENT = {
    "commitment_due": "follow_up",
    "open_thread": "follow_up",
    "weather": "share",
}

DRIVE_RECORD_THRESHOLD = 0.5


@dataclass(frozen=True)
class IntentDraft:
    type: str
    why: dict
    plan: dict


@dataclass(frozen=True)
class Wait:
    reason: str


@dataclass(frozen=True)
class Defer:
    until: datetime
    reason: str
    candidate: IntentDraft | None = None


def _build_intent(top, opportunities, drives) -> IntentDraft:
    drive_kinds = []
    for d in drives or []:
        try:
            if float(getattr(d, "intensity", 0.0)) >= DRIVE_RECORD_THRESHOLD:
                drive_kinds.append(getattr(d, "kind", ""))
        except (TypeError, ValueError):
            continue
    secondary = [{"kind": o.kind, "payload": dict(o.payload or {})}
                 for o in opportunities if o is not top]
    return IntentDraft(
        type=KIND_TO_INTENT.get(top.kind, "share"),
        why={
            "opportunity_kind": top.kind,
            "opportunity_payload": dict(top.payload or {}),
            "score": round(top.score, 4),
            "drives": drive_kinds,
        },
        plan={"primary": dict(top.payload or {}), "secondary": secondary},
    )


def plan(*, opportunities, drives=None, constraints=None, now,
         config: dict | None = None) -> IntentDraft | Wait | Defer:
    """生成本轮建议：Intent（该做）/ Wait（无所可为）/ Defer（该做但现在不行）。"""
    constraints = constraints or {}
    opps = list(opportunities or [])
    eligible = [o for o in opps if o.score >= MIN_ELIGIBLE_SCORE]

    top = eligible[0] if eligible else None

    if constraints.get("quiet"):
        until = constraints.get("quiet_until")
        candidate = _build_intent(top, opps, drives) if top is not None else None
        return Defer(until=until, reason="quiet_hours", candidate=candidate)

    if top is None:
        return Wait(reason="no_eligible_opportunity")

    return _build_intent(top, opps, drives)
