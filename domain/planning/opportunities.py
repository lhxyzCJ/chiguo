"""domain.planning.opportunities — 机会发现（观测 × 状态 → 可行动契机）。

纯函数、无 IO、确定性输出顺序（分数降序，同分按 kind 字典序）。
输入三路事实，各自可独立缺席：
- observations：世界观测（sources/*.observe() 的产物）；
- commitments：未完成承诺（due 已过且在宽限窗内 → commitment_due）；
- threads：未结束话题（长时间未互动 → open_thread）。

评分（0..1）语义：
- novelty 新异度（当前恒 1.0——去重/历史抑制留给后续阶段）；
- relevance 与用户的相关度；urgency 时效压力；emotional_affordance 情感可供性。
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

# 观测类型 → 机会规则（kind/评分）
OBSERVATION_RULES = {
    "weather.changed": {"kind": "weather", "relevance": 0.4, "urgency": 0.1,
                        "emotional_affordance": 0.3},
}

DEFAULT_COMMITMENT_GRACE_HOURS = 24.0   # 到期后的宽限窗，超窗不再产机会
DEFAULT_OPEN_THREAD_STALE_HOURS = 6.0   # 话题静默多久算「可续聊」


@dataclass(frozen=True)
class OpportunityDraft:
    kind: str
    novelty: float
    relevance: float
    urgency: float
    emotional_affordance: float
    expires_at: datetime | None
    observation_event_id: str | None
    payload: dict

    @property
    def score(self) -> float:
        return (self.urgency * 0.5 + self.relevance * 0.3
                + self.emotional_affordance * 0.2)


def _as_dt(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, str) and v:
        try:
            return datetime.fromisoformat(v)
        except ValueError:
            return None
    return None


def discover_opportunities(*, observations, commitments, threads, now,
                           config: dict | None = None) -> list[OpportunityDraft]:
    """从观测/承诺/话题三路事实发现机会（确定性排序）。"""
    cfg = config or {}
    try:
        grace_h = float(cfg.get("commitment_grace_hours",
                                DEFAULT_COMMITMENT_GRACE_HOURS))
    except (TypeError, ValueError):
        grace_h = DEFAULT_COMMITMENT_GRACE_HOURS
    try:
        stale_h = float(cfg.get("open_thread_stale_hours",
                                DEFAULT_OPEN_THREAD_STALE_HOURS))
    except (TypeError, ValueError):
        stale_h = DEFAULT_OPEN_THREAD_STALE_HOURS

    drafts: list[OpportunityDraft] = []

    for ob in observations or []:
        if ob.expires_at is not None and ob.expires_at < now:
            continue
        rule = OBSERVATION_RULES.get(ob.type)
        if rule is None:
            continue  # 未登记观测类型只作环境事实，不单独产机会
        drafts.append(OpportunityDraft(
            kind=rule["kind"], novelty=1.0, relevance=rule["relevance"],
            urgency=rule["urgency"], emotional_affordance=rule["emotional_affordance"],
            expires_at=ob.expires_at, observation_event_id=None,
            payload=dict(ob.payload or {})))

    for c in commitments or []:
        if getattr(c, "status", None) != "open":
            continue
        due = _as_dt(getattr(c, "due_at", None))
        if due is None or due > now:
            continue
        if (now - due) > timedelta(hours=grace_h):
            continue  # 过期太久（宽限窗外）——旧事不再追
        drafts.append(OpportunityDraft(
            kind="commitment_due", novelty=1.0, relevance=0.9, urgency=0.8,
            emotional_affordance=0.7,
            expires_at=due + timedelta(hours=grace_h),
            observation_event_id=None,
            payload={"commitment_id": getattr(c, "id", None),
                     "subject": getattr(c, "subject", ""),
                     "due_at": due.isoformat()}))

    for t in threads or []:
        if getattr(t, "state", None) != "open":
            continue
        last = _as_dt(getattr(t, "last_interaction_at", None)) \
            or _as_dt(getattr(t, "opened_at", None))
        if last is None or (now - last) < timedelta(hours=stale_h):
            continue
        drafts.append(OpportunityDraft(
            kind="open_thread", novelty=1.0, relevance=0.6, urgency=0.3,
            emotional_affordance=0.3, expires_at=None,
            observation_event_id=None,
            payload={"thread_id": getattr(t, "id", None),
                     "subject": getattr(t, "subject", "")}))

    drafts.sort(key=lambda d: (-d.score, d.kind))
    return drafts
