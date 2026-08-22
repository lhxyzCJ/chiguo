# ============================================================
# chiguo_state_models.py — 情绪/冷却状态模型抽离（T10）
# 从状态主文件剥离的纯模型层：零反向依赖（不 import 状态主文件），
# 仅依赖 chiguo_math / chiguo_time 与 stdlib，保持单向边。
# StatePersistence 的 personality/circadian 序列化仍在 state 侧，
# 仅纯逻辑保留在叶子；此处为 Cooldown/Emotion 的纯模型。
# ============================================================

import json
import types
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from chiguo_math import in_quiet_window
from chiguo_time import CST

# ── Q7 (#79/#260): reminder 去重标记（last_triggered_at）跨进程持久化。
_MEMORY_MARKER_KEYS = ("last_triggered_at",)


def _memory_dedup_key(mem: dict) -> str:
    """记忆去重/内容键：排除运行时标记字段后的稳定标识（跨进程可匹配）。"""
    key = {k: v for k, v in mem.items() if k not in _MEMORY_MARKER_KEYS}
    return json.dumps(key, sort_keys=True, ensure_ascii=False)


@dataclass
class ChiguoEmotion:
    loneliness: float = 15.0
    affection: float = 55.0
    anxiety: float = 40.0
    energy: float = 85.0
    tsundere_index: float = 70.0
    loneliness_rate: float = 0.0    # Δloneliness/hour
    anxiety_rate: float = 0.0       # Δanxiety/hour
    baseline_loneliness: float = 100.0  # v1.11 ④: 长期收敛目标（默认=现 tick target → 恒等）
    baseline_anxiety: float = 100.0     # v1.11 ④
    baseline_affection: float = 0.0     # v1.11 ④

    @property
    def neediness(self) -> float:
        return self.loneliness * (1 - self.tsundere_index / 200) * (self.anxiety / 100)

    @property
    def dominant_layer(self) -> str:
        if self.anxiety > 70 or self.loneliness > 80:
            return "kernel"
        elif self.loneliness > 50:
            return "middle"
        else:
            return "shell"

    def clamp(self):
        self.loneliness = max(0, min(100, self.loneliness))
        self.affection = max(5, min(100, self.affection))
        self.anxiety = max(0, min(100, self.anxiety))
        self.energy = max(0, min(100, self.energy))
        self.tsundere_index = max(10, min(95, self.tsundere_index))


def _coerce_dataclass_fields(fields: dict, cls) -> dict:
    """v11: dataclass 数值字段(annotation 为 int/float)类型强转。"""
    out = dict(fields)
    for name, fdef in cls.__dataclass_fields__.items():
        ann = fdef.type
        nullable = False
        if isinstance(ann, types.UnionType):
            args = getattr(ann, "__args__", ())
            if len(args) == 2 and type(None) in args:
                base = args[0] if args[1] is type(None) else args[1]
                if base in (int, float):
                    ann = base
                    nullable = True
        if ann not in (int, float, "int", "float"):
            continue
        if name not in out:
            continue
        val = out[name]
        if val is None:
            if nullable:
                continue
            out[name] = fdef.default
            continue
        if isinstance(val, (int, float)):
            continue
        try:
            if ann is int or ann == "int":
                out[name] = int(float(val))
            else:
                out[name] = float(val)
        except (ValueError, TypeError, OverflowError):
            out[name] = fdef.default
    return out


# v1.11 ④: 情绪基线全局默认（= 原 tick 收敛 target：loneliness/anxiety→100、affection→0）
BASELINE_DEFAULTS = {"loneliness": 100.0, "anxiety": 100.0, "affection": 0.0}


# ── B1: 事件类型化情绪 delta（event_delta_enabled 默认 False → 恒等，可灰度）──
EVENT_DELTA = {
    "praise":        {"loneliness": -3.0, "affection": 2.0},
    "criticism":     {"loneliness": 2.0, "anxiety": 3.0},
    "contradiction": {"anxiety": 4.0},
    "comfort":       {"anxiety": -3.0, "affection": 1.5},
    "new_topic":     {"affection": 1.0},
    "question":      {"affection": 0.8},
    "complaint":     {"anxiety": 2.0},
}

# B1: 事件类型宽松匹配别名（归一化后的原始串 → 规范事件类型）
EVENT_TYPE_SYNONYMS = {
    "praise": "praise", "夸": "praise", "夸奖": "praise", "表扬": "praise",
    "赞": "praise", "赞美": "praise",
    "criticism": "criticism", "批评": "criticism", "责备": "criticism",
    "骂": "criticism", "指责": "criticism",
    "contradiction": "contradiction", "反驳": "contradiction", "抬杠": "contradiction",
    "comfort": "comfort", "安慰": "comfort", "哄": "comfort", "安抚": "comfort",
    "new_topic": "new_topic", "newtopic": "new_topic", "换话题": "new_topic", "新话题": "new_topic",
    "question": "question", "提问": "question", "问": "question",
    "complaint": "complaint", "抱怨": "complaint", "吐槽": "complaint",
}


def emotion_tag_snapshot(emotion) -> dict:
    """B2: 情绪 → 离散档标签（写侧打标用，读侧加权比对）。"""
    def _level(v: float) -> str:
        if v <= 30:
            return "low"
        if v >= 70:
            return "high"
        return "mid"
    return {
        "loneliness": _level(emotion.loneliness),
        "affection": _level(emotion.affection),
        "anxiety": _level(emotion.anxiety),
        "energy": _level(emotion.energy),
    }


# F-A15-002: refunded msg_id 有界 FIFO 上限——记录最近已退款的 msg_id，防越窗口
REFUND_FIFO_MAX = 200


@dataclass
class CooldownState:
    last_message_at: str | None = None
    last_user_message_at: str | None = None
    messages_today: int = 0
    messages_without_reply: int = 0
    current_date: str = ""
    morning_sent: bool = False
    night_sent: bool = False
    trigger_history: list[str] = field(default_factory=list)
    event_timestamps: list[dict] = field(default_factory=list)
    reply_latencies: list[float] = field(default_factory=list)
    busy_suppress_until: str | None = None
    held_count: int = 0
    accumulated_lambda: float = 0.0
    last_user_msg_length: int | None = None
    last_crash_at: str | None = None
    crash_count_48h: int = 0
    crash_timestamps: list[str] = field(default_factory=list)
    last_longing_break_at: str | None = None
    recv_dedup: dict | None = None
    drop_events: list[dict] = field(default_factory=list)
    user_mood: dict | None = None
    reply_stats: dict = field(default_factory=dict)
    reply_pending: list[str] = field(default_factory=list)
    consolidate_last_at: str | None = None
    refunded_msg_ids: list[str] = field(default_factory=list)

    def get_last_message_at(self) -> str | None:
        return self.last_message_at

    def get_last_user_message_at(self) -> str | None:
        return self.last_user_message_at

    def get_messages_today(self) -> int:
        return self.messages_today

    def get_messages_without_reply(self) -> int:
        return self.messages_without_reply

    def get_trigger_history(self) -> list[str]:
        return self.trigger_history

    def get_event_timestamps(self) -> list[dict]:
        return self.event_timestamps

    def get_reply_latencies(self) -> list[float]:
        return self.reply_latencies

    def get_busy_suppress_until(self) -> str | None:
        return self.busy_suppress_until

    def get_held_count(self) -> int:
        return self.held_count

    def get_accumulated_lambda(self) -> float:
        return self.accumulated_lambda

    def get_recv_dedup(self) -> dict | None:
        return self.recv_dedup

    def get_user_mood(self) -> dict | None:
        return self.user_mood

    def get_reply_stats(self) -> dict:
        return self.reply_stats

    def get_consolidate_last_at(self) -> str | None:
        return self.consolidate_last_at

    def is_morning_sent(self) -> bool:
        return self.morning_sent

    def is_night_sent(self) -> bool:
        return self.night_sent

    def get_current_date(self) -> str:
        return self.current_date

    def append_trigger_history(self, trigger_type: str, max_len: int = 6):
        self.trigger_history.append(trigger_type)
        if len(self.trigger_history) > max_len:
            self.trigger_history = self.trigger_history[-max_len:]

    def mark_morning_sent(self):
        self.morning_sent = True

    def mark_night_sent(self):
        self.night_sent = True

    def increment_held(self) -> int:
        self.held_count += 1
        return self.held_count

    def set_accumulated_lambda(self, value: float):
        self.accumulated_lambda = float(value)

    def set_consolidate_last_at(self, value: str | None):
        self.consolidate_last_at = value

    def set_recv_dedup(self, dedup: dict | None):
        self.recv_dedup = dedup

    def set_last_message_at(self, value: str | None):
        self.last_message_at = value

    def set_last_user_message_at(self, value: str | None):
        self.last_user_message_at = value

    def set_user_mood(self, value: dict | None):
        self.user_mood = value

    def __post_init__(self):
        self._quiet_start = 0
        self._quiet_end = 8

    def set_quiet_window(self, start: int, end: int):
        try:
            start, end = int(start), int(end)
        except (ValueError, TypeError):
            start, end = 0, 8
        if not (0 <= start <= 23 and 0 <= end <= 23):
            start, end = 0, 8
        self._quiet_start = start
        self._quiet_end = end

    def quiet_window(self) -> tuple[int, int]:
        return self._quiet_start, self._quiet_end

    def silent_hours(self, now: datetime, wall: bool = False) -> float:
        if not self.last_user_message_at:
            return 999.0
        try:
            last = datetime.fromisoformat(self.last_user_message_at)
        except (ValueError, TypeError):
            return 999.0
        if last.tzinfo is None:
            last = last.replace(tzinfo=CST)
        raw = (now - last).total_seconds() / 3600
        if wall:
            return max(0.0, raw)
        sleep_hours = self._sleep_hours_in_range(last, now)
        return max(0.0, raw - sleep_hours)

    def _sleep_hours_in_range(self, start: datetime, end: datetime) -> float:
        qs, qe = self._quiet_start, self._quiet_end
        total = 0.0
        cur = start
        guard = 0
        while cur < end and guard < 4000:
            day = cur.replace(hour=0, minute=0, second=0, microsecond=0)
            ws = day.replace(hour=qs, minute=0, second=0, microsecond=0)
            we = day.replace(hour=qe, minute=0, second=0, microsecond=0)
            if qe < qs:
                if cur < we:
                    tail_start = max(cur, day)
                    tail_end = min(end, we)
                    if tail_start < tail_end:
                        total += (tail_end - tail_start).total_seconds() / 3600
                    cur = we
                    guard += 1
                    continue
                we = we + timedelta(days=1)
            if we <= cur:
                cur = ws + timedelta(days=1)
                guard += 1
                continue
            if ws < end and we > cur:
                overlap_start = max(cur, ws)
                overlap_end = min(end, we)
                total += (overlap_end - overlap_start).total_seconds() / 3600
            cur = we
            guard += 1
        return total

    def minutes_since_last_message(self, now: datetime) -> float | None:
        if not self.last_message_at:
            return 999.0
        try:
            last = datetime.fromisoformat(self.last_message_at)
            if last.tzinfo is None:
                last = last.replace(tzinfo=CST)
            delta = (now - last).total_seconds() / 60
            return max(0.0, delta)
        except (ValueError, TypeError):
            return None

    def is_busy_suppressed(self, now: datetime) -> bool:
        if not self.busy_suppress_until:
            return False
        try:
            until = datetime.fromisoformat(self.busy_suppress_until)
            return now < until
        except (ValueError, TypeError):
            return False
