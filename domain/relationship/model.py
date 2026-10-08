"""domain.relationship.model — 纯函数式关系域模型（v2 Phase 4，architecture-v2 §3.1/§3.9）。

关系域承载「迟菓 × 用户」之间的长期状态，由事件驱动更新、按时间自然衰减。
纯函数 + frozen dataclass：无 IO、无全局状态、不依赖 config 对象（动力学常数为
模块常量，Phase 5/6 若需参数化再收敛到 config）。

字段域（docstring 契约）：
- closeness            [0,1] 长期亲近度（温暖互动缓慢抬升，冷淡微降）
- trust                [0,1] 长期信任（只随正向互动抬升；单次冷淡不惩罚）
- familiarity          [0,1] 熟悉度（互动总量累积，只升不降，步长极小）
- recent_warmth        [0,1] 近期温暖度（按消息 warmth 抬升，72h 半衰回归 0）
- recent_tension       [0,1] 近期紧张度（冷淡回复/久无回复抬升，48h 半衰回归 0）
- interaction_rhythm   [0,1] 近期互动密度代理（消息事件向 1 收敛，24h 半衰回归 0）
- initiative_balance   [-1,1] 主动平衡：-1 = 用户总是先开口；+1 = 迟菓总是先开口；
                       0 = 均衡。用户回复向 -1 移动，迟菓发送向 +1 移动（步长 0.15）
- shared_history_depth [0,1] 共享经历深度（互动累积的里程碑代理，只升不降）

事件语义：
- `message.received`：按 payload.warmth（[-1,1] 钳制）更新 recent_warmth（正）或
  recent_tension（负）；initiative_balance 向用户侧移动；familiarity/深度缓增；
  interaction_rhythm 上升。无 warmth 视为中性回复（只做计数类更新）。
- `message.sent`：initiative_balance 向自己侧移动；payload.silent_hours ≥ 24 时
  recent_tension 缓增（0.1/次，累积到 1 封顶）——「长时间无回复」由 reducer 从消息
  历史计算后显式传入。未送达不算主动：`message.delivery_failed` 全字段恒等。
- 未知事件类型 → 恒等（前向兼容：新事件由各自 reducer 扩展）。

`apply_event` 的 `now` 为事件时间锚点（当前数值更新不依赖绝对时间；保留以满足
Phase 5 reducer 的因果链签名）。`decay(state, hours)` 只回归近期维度，长期维度不
随时间衰减（需事件显式更新）。
"""

import math
from dataclasses import dataclass, replace

from chiguo_math import decay as _half_life_decay

# ── 事件效应步长（模块常量，语义见模块 docstring）──
INITIATIVE_STEP = 0.15       # 单条消息对 initiative_balance 的移动量
FAMILIARITY_STEP = 0.002     # 单条用户消息的熟悉度增量（缓慢）
SHARED_HISTORY_STEP = 0.001  # 单条用户消息的共享经历深度增量（极缓）
WARMTH_GAIN = 0.3            # 正 warmth → recent_warmth 增量系数
TENSION_COLD_GAIN = 0.3      # 负 warmth → recent_tension 增量系数
TENSION_SILENT_GAIN = 0.1    # 久无回复时每条主动消息的 tension 增量
SILENCE_TENSION_HOURS = 24.0  # 「长时间无回复」阈值
CLOSENESS_GAIN = 0.02        # 正 warmth → closeness 增量系数（冷淡按同系数微降）
TRUST_GAIN = 0.01            # 正 warmth → trust 增量系数
RHYTHM_GAIN = 0.05           # 消息事件向 1 收敛的 EMA 系数

# ── 近期维度半衰期（小时）──
RECENT_WARMTH_HALF_LIFE = 72.0
RECENT_TENSION_HALF_LIFE = 48.0
INTERACTION_RHYTHM_HALF_LIFE = 24.0


def _num(value, default: float) -> float:
    """数值解析：None/非数值/NaN/inf → 默认（与 affect 域同一容错口径）。"""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _u01(v: float) -> float:
    return max(0.0, min(1.0, v))


@dataclass(frozen=True)
class RelationshipState:
    """关系域快照（frozen）。各字段域见模块 docstring。"""

    closeness: float = 0.5
    trust: float = 0.5
    familiarity: float = 0.0
    recent_warmth: float = 0.0
    recent_tension: float = 0.0
    interaction_rhythm: float = 0.0
    initiative_balance: float = 0.0
    shared_history_depth: float = 0.0


def initial() -> RelationshipState:
    """中性初值：closeness/trust 0.5，其余 0（无历史、无倾向）。"""
    return RelationshipState()


def apply_event(state: RelationshipState, event_type: str, *, now,
                payload: dict | None = None) -> RelationshipState:
    """事件 → 关系状态（纯函数；未知事件恒等）。"""
    p = payload if isinstance(payload, dict) else {}

    if event_type == "message.received":
        warmth = max(-1.0, min(1.0, _num(p.get("warmth", 0.0), 0.0)))
        recent_warmth = state.recent_warmth
        recent_tension = state.recent_tension
        closeness = state.closeness
        trust = state.trust
        if warmth > 0:
            recent_warmth = _u01(recent_warmth + WARMTH_GAIN * warmth)
            closeness = _u01(closeness + CLOSENESS_GAIN * warmth)
            trust = _u01(trust + TRUST_GAIN * warmth)
        elif warmth < 0:
            recent_tension = _u01(recent_tension + TENSION_COLD_GAIN * abs(warmth))
            closeness = _u01(closeness - CLOSENESS_GAIN * abs(warmth))
            # trust 不动：长期信任不经单次冷淡摇摆
        return replace(
            state,
            closeness=closeness,
            trust=trust,
            familiarity=_u01(state.familiarity + FAMILIARITY_STEP),
            recent_warmth=recent_warmth,
            recent_tension=recent_tension,
            interaction_rhythm=state.interaction_rhythm
            + (1.0 - state.interaction_rhythm) * RHYTHM_GAIN,
            initiative_balance=max(-1.0, state.initiative_balance - INITIATIVE_STEP),
            shared_history_depth=_u01(state.shared_history_depth + SHARED_HISTORY_STEP),
        )

    if event_type == "message.sent":
        silent_hours = _num(p.get("silent_hours", 0.0), 0.0)
        tension_gain = TENSION_SILENT_GAIN if silent_hours >= SILENCE_TENSION_HOURS else 0.0
        return replace(
            state,
            recent_tension=_u01(state.recent_tension + tension_gain),
            interaction_rhythm=state.interaction_rhythm
            + (1.0 - state.interaction_rhythm) * RHYTHM_GAIN,
            initiative_balance=min(1.0, state.initiative_balance + INITIATIVE_STEP),
        )

    if event_type == "message.delivery_failed":
        # 未送达不算主动：不改 warmth、不改 initiative，全字段恒等
        return state

    return state


def decay(state: RelationshipState, hours: float) -> RelationshipState:
    """时间衰减：recent_warmth / recent_tension / interaction_rhythm 向 0 半衰回归。

    长期维度（closeness/trust/familiarity/initiative_balance/shared_history_depth）
    不随时间衰减，只能被事件显式更新。hours ≤ 0 / None / NaN → 恒等。
    """
    h = _num(hours, 0.0)
    if h <= 0:
        return state
    return replace(
        state,
        recent_warmth=_half_life_decay(state.recent_warmth, h, RECENT_WARMTH_HALF_LIFE),
        recent_tension=_half_life_decay(state.recent_tension, h, RECENT_TENSION_HALF_LIFE),
        interaction_rhythm=_half_life_decay(state.interaction_rhythm, h,
                                            INTERACTION_RHYTHM_HALF_LIFE),
    )
