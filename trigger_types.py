# ============================================================
# trigger_types.py — 触发类型单一事实来源（枚举）
# T7·Q3 (#265)：集中登记全部触发类型，消除 replan/daemon/composer/trigger
# 四处裸字符串比对漂移。StrEnum 值 == 原始字符串，替换比较行为不变。
# ============================================================

from enum import StrEnum


class TriggerType(StrEnum):
    """全部触发类型（单一事实来源）。

    值即历史字符串字面量，StrEnum 成员与对应 str 相等（"comfort" == TriggerType.COMFORT），
    替换比较/取 key 均行为不变。禁止在别处新增散落字符串类型。
    """

    # ── 仪式类（A3/A4/A5 豁免日程乘数/高段必选/退场禁发）──
    SPECIAL = "special"
    MORNING = "morning"
    NIGHT = "night"
    MEMORY = "memory"
    MEAL = "meal"
    FOLLOW_UP = "follow_up"

    # ── 情绪类（A3 日程乘数 / A4 activation / A5 退场作用于此集合）──
    LONELY_LOW = "lonely_low"
    LONELY_MID = "lonely_mid"
    LONELY_HIGH = "lonely_high"
    ANXIETY = "anxiety"
    PLAYFUL = "playful"
    REFLECT = "reflect"
    LONGING = "longing"
    COMFORT = "comfort"


# 情绪类触发集合：A3 日程乘数、A4 activation、A5 backing_off 只作用于情绪类。
EMOTION_TRIGGERS = frozenset({
    TriggerType.LONELY_LOW,
    TriggerType.LONELY_MID,
    TriggerType.LONELY_HIGH,
    TriggerType.ANXIETY,
    TriggerType.PLAYFUL,
    TriggerType.REFLECT,
    TriggerType.LONGING,
    TriggerType.COMFORT,
})

# 仪式类触发集合：豁免 A3/A4/A5。
RITUAL_TRIGGERS = frozenset({
    TriggerType.SPECIAL,
    TriggerType.MORNING,
    TriggerType.NIGHT,
    TriggerType.MEMORY,
    TriggerType.MEAL,
    TriggerType.FOLLOW_UP,
})

# 全部真实触发类型的字符串值集合（供 replan 校验产出 ⊆ 枚举）。
TRIGGER_TYPE_VALUES = frozenset(t.value for t in TriggerType)


# replan 合法 trigger_scale key：全部真实触发类型 + "default"。
# "default" 不是真实触发类型，而是作用于缺席类型的全局缩放键
# （chiguo_trigger 单点缩放 scale.get(type, scale.get("default", 1.0))），
# 仅作 replan scale key 白名单，不进入 TriggerType。
REPLAN_SCALE_KEYS = TRIGGER_TYPE_VALUES | {"default"}
