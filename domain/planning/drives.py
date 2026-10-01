"""domain.planning.drives — 驱动力评估（affect/relationship/机会 → 「当前想做什么」）。

Drive 是情绪与行为之间的缓冲层：情绪不直接触发发送，只影响 drive 强度；
planner 再把 opportunity + drive 变成 intent（architecture-v2 §3.8）。

最小线性映射（模块常量，便于后续调参）：
- reconnect           = affect.loneliness / 100          （孤独 → 想重新连接）
- care                = affect.affection / 100 × (0.5 + relationship.closeness / 2)
- playfulness         = affect.energy / 100 × 0.4
- unfinished_business = 0.8（存在 kind == "commitment_due" 的机会）否则不产
- curiosity           = relationship.familiarity / 100 × 0.3

输出 intensity ∈ [0,1]（越界 clamp），<= MIN_INTENSITY 过滤；
按 intensity 降序，同分按 kind 字典序。affect / relationship 为 duck-type
（dataclass 即可，只要有属性）；缺属性 / None / NaN / inf → 跳过该驱动，不崩。
"""
import math
from dataclasses import dataclass
from datetime import datetime

# ── 线性映射常量（调参入口）──
RECONNECT_SCALE = 1.0             # reconnect = loneliness/100 × 1.0
CARE_BASE = 0.5                   # care = affection/100 × (0.5 + closeness/2)
CARE_CLOSENESS_SCALE = 0.5
PLAYFULNESS_SCALE = 0.4           # playfulness = energy/100 × 0.4
UNFINISHED_BUSINESS_INTENSITY = 0.8
CURIOSITY_SCALE = 0.3             # curiosity = familiarity/100 × 0.3

MIN_INTENSITY = 0.05              # 过滤阈值（含）


@dataclass(frozen=True)
class DriveDraft:
    """一条驱动力评估结果：kind + intensity(0..1) + inputs（依据快照，可解释性）。"""

    kind: str
    intensity: float
    inputs: dict


def _num_attr(obj, name: str) -> float | None:
    """从 duck-type 对象取有限数值；缺属性/None/非数值/NaN/inf → None。"""
    try:
        value = getattr(obj, name)
    except AttributeError:
        return None
    if isinstance(value, bool):  # bool 是 int 子类，语义上不是情绪数值
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _u01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _field(obj, name: str):
    """机会字段读取（对象属性优先，dict 兜底）；缺 → None。"""
    value = getattr(obj, name, None)
    if value is None and isinstance(obj, dict):
        value = obj.get(name)
    return value


def _commitment_due_inputs(opportunities) -> dict | None:
    """汇总 commitment_due 机会（无 → None）；inputs 只读 kind/score，不解释机会语义。"""
    count = 0
    best_score: float | None = None
    for opp in opportunities or []:
        if _field(opp, "kind") != "commitment_due":
            continue
        count += 1
        raw = _field(opp, "score")
        try:
            score = float(raw)
        except (TypeError, ValueError, OverflowError):
            score = None
        if score is not None and math.isfinite(score):
            if best_score is None or score > best_score:
                best_score = score
    if count == 0:
        return None
    return {"opportunity_kind": "commitment_due", "count": count,
            "best_score": best_score}


def evaluate_drives(*, affect, relationship, opportunities=None,
                    now: datetime) -> list[DriveDraft]:
    """评估当前驱动力（详见模块 docstring；now 为评估时间锚点，映射本身与时刻无关）。"""
    drafts: list[DriveDraft] = []

    loneliness = _num_attr(affect, "loneliness")
    if loneliness is not None:
        drafts.append(DriveDraft(
            "reconnect", _u01(loneliness / 100.0 * RECONNECT_SCALE),
            {"loneliness": loneliness}))

    affection = _num_attr(affect, "affection")
    closeness = _num_attr(relationship, "closeness")
    if affection is not None and closeness is not None:
        drafts.append(DriveDraft(
            "care",
            _u01(affection / 100.0 * (CARE_BASE + closeness * CARE_CLOSENESS_SCALE)),
            {"affection": affection, "closeness": closeness}))

    energy = _num_attr(affect, "energy")
    if energy is not None:
        drafts.append(DriveDraft(
            "playfulness", _u01(energy / 100.0 * PLAYFULNESS_SCALE),
            {"energy": energy}))

    due_inputs = _commitment_due_inputs(opportunities)
    if due_inputs is not None:
        drafts.append(DriveDraft("unfinished_business",
                                 UNFINISHED_BUSINESS_INTENSITY, due_inputs))

    familiarity = _num_attr(relationship, "familiarity")
    if familiarity is not None:
        drafts.append(DriveDraft(
            "curiosity", _u01(familiarity / 100.0 * CURIOSITY_SCALE),
            {"familiarity": familiarity}))

    drafts = [d for d in drafts if d.intensity > MIN_INTENSITY]
    drafts.sort(key=lambda d: (-d.intensity, d.kind))
    return drafts
