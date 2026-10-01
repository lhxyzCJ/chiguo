"""domain.affect.model — 纯函数式情绪域模型（v2 Phase 4）。

从 ChiguoState 上帝类剥离的「情绪引擎」：无文件 IO、无全局状态、不依赖 config
对象（构造时传入 dict）。所有状态以 frozen dataclass `AffectState` 显式进出，
Phase 5/6 的 reducer 由事件驱动调用本模块并负责持久化。

数值语义与旧引擎对齐，移植来源（均为只读参照）：
- `state/emotion.py::EmotionMixin.tick`：四路 elastic_recover（loneliness/anxiety/
  affection/energy）+ 半衰期情境修正（静默 >24h、节假日、课表）+ tsundere 回归 +
  A2 交互矩阵 + OU 噪声 + 基线淡忘 + rate 计算 + clamp。
- `state/interaction.py::InteractionMixin.on_user_message / on_character_message /
  refund_send`：只取情绪数值部分；cooldown/日额度/Hawkes 事件/生物钟/人格演化
  等非情绪职责留在 reducer 层（本模块不复制）。
- `state/mood.py::MoodMixin._apply_analysis_impact`：EVENT_DELTA（B1）、
  warmth/effort/attention 微调（惯性阻尼 impact_inertia）、user_mood 感知消费与
  TTL 重放（①）、回复速度倍率。
- `state/personality.py::PersonalityMixin._adapt_on_reply → update_emotion_baseline`：
  事件驱动基线漂移（baseline_drift_rate 默认 0 → 恒等）。

纯依赖注入的显式参数（旧实现从 ChiguoState 其他组件读取，本模块不引用）：
- `silent_hours`（旧 `cooldown.silent_hours(now)`）
- `is_holiday` / `is_school_day`（旧 `holiday_parser`）、`in_class` / `class_load`
  （旧 `schedule_status(now)`）
- `tsundere_baseline`（旧 `personality.tsundere_intensity`）
- `anxiety_sensitivity`（旧 `personality.anxiety_sensitivity()`）
- `damp`（旧 `_reply_damp` 的 A10 饱和阻尼系数，由 reducer 按事件窗口计算）
- `noise_state`（OU 噪声累积态，旧实例私有 `_noise_x`）→ 落在 AffectState 的
  `noise_loneliness` / `noise_anxiety` 字段显式随状态传递；`noise_state` 属性
  提供旧字典视图。生产 reducer 应通过 `rng` 参数注入跨 tick 复用的 Random 实例，
  否则每次调用按 `[emotion].noise_seed` 重新播种（纯函数无法持有实例，噪声序列
  在 tick 间不连续）。

数学机制直接复用 `chiguo_math` 纯函数（elastic_recover / apply_interaction_matrix /
ou_step / noise_cap / decay / impact_inertia / user_mood_impact / baseline_shift_of /
mood_fresh），未重写、未“改进”算法。非法输入（None/NaN/inf/类型错误）统一回退默认，
输出一律 clamp 到合法域（与旧 `_coerce` 容错风格一致）。
"""

import math
import random
import re
from dataclasses import dataclass, replace
from datetime import datetime

from chiguo_math import (
    MOOD_DELTA,
    apply_interaction_matrix,
    baseline_shift_of,
    decay,
    elastic_recover,
    impact_inertia,
    mood_fresh,
    noise_cap,
    ou_step,
    user_mood_impact,
)
from chiguo_state_models import BASELINE_DEFAULTS, EVENT_DELTA, EVENT_TYPE_SYNONYMS
from chiguo_time import CST

# 与 ChiguoEmotion 一致的十个数值字段（对账口径；不含噪声/感知缓存等扩展字段）
_AFFECT_FLOATS = (
    "loneliness", "affection", "anxiety", "energy", "tsundere_index",
    "loneliness_rate", "anxiety_rate",
    "baseline_loneliness", "baseline_anxiety", "baseline_affection",
)

# 旧 ChiguoEmotion.clamp() 的合法域
_CLAMP_BOUNDS = {
    "loneliness": (0.0, 100.0),
    "affection": (5.0, 100.0),
    "anxiety": (0.0, 100.0),
    "energy": (0.0, 100.0),
    "tsundere_index": (10.0, 95.0),
}


def _num(value, default: float | None) -> float | None:
    """数值解析：None/非数值/NaN/inf/溢出 → 默认（旧 `_coerce` 容错风格的收敛版）。"""
    try:
        v = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(v):
        return default
    return v


def _emotion_cfg(config) -> dict:
    cfg = config.get("emotion", {}) if isinstance(config, dict) else {}
    return cfg if isinstance(cfg, dict) else {}


def _personality_cfg(config) -> dict:
    cfg = config.get("personality", {}) if isinstance(config, dict) else {}
    return cfg if isinstance(cfg, dict) else {}


def _trigger_cfg(config) -> dict:
    cfg = config.get("trigger", {}) if isinstance(config, dict) else {}
    return cfg if isinstance(cfg, dict) else {}


def _clamp_in_place(d: dict) -> None:
    """就地 clamp 到旧 ChiguoEmotion.clamp() 的合法域（NaN 已在 `_num` 被挡）。

    只钳制 dict 中出现的字段（apply_character_send / refund_send 只携带被改动的维度）。
    """
    for name, (lo, hi) in _CLAMP_BOUNDS.items():
        if name in d:
            d[name] = max(lo, min(hi, d[name]))


@dataclass(frozen=True)
class AffectState:
    """情绪域快照（frozen）：十个维度与旧 ChiguoEmotion 完全一致，便于 Phase 4 对账。

    - loneliness / affection / anxiety / energy / tsundere_index：0..100（tsundere 10..95；
      affection 下限 5），与旧 clamp 相同；
    - loneliness_rate / anxiety_rate：Δ/小时（仅 tick 推进时更新，hours>0.01）；
    - baseline_loneliness / baseline_anxiety / baseline_affection：长期收敛目标
      （漂移域 [默认±baseline_max_drift]，淡忘半衰期默认 720h）；
    - noise_loneliness / noise_anxiety：OU 噪声累积态（旧 `_noise_x`，显式随状态传递）；
    - user_mood：用户情绪感知缓存（旧 `cooldown.user_mood`，TTL 由 mood_fresh 判定）。
    """

    loneliness: float = 15.0
    affection: float = 55.0
    anxiety: float = 40.0
    energy: float = 85.0
    tsundere_index: float = 70.0
    loneliness_rate: float = 0.0
    anxiety_rate: float = 0.0
    baseline_loneliness: float = 100.0
    baseline_anxiety: float = 100.0
    baseline_affection: float = 0.0
    noise_loneliness: float = 0.0
    noise_anxiety: float = 0.0
    user_mood: dict | None = None

    @property
    def neediness(self) -> float:
        """与旧 ChiguoEmotion.neediness 相同公式。"""
        return self.loneliness * (1 - self.tsundere_index / 200) * (self.anxiety / 100)

    @property
    def dominant_layer(self) -> str:
        """与旧 ChiguoEmotion.dominant_layer 相同阈值：kernel/middle/shell。"""
        if self.anxiety > 70 or self.loneliness > 80:
            return "kernel"
        elif self.loneliness > 50:
            return "middle"
        else:
            return "shell"

    @property
    def noise_state(self) -> dict:
        """旧 `_noise_x` 等价视图（供 reducer 持久化/对账）。"""
        return {"loneliness": self.noise_loneliness, "anxiety": self.noise_anxiety}


def initial(config: dict) -> AffectState:
    """初值：[emotion] 段初始值，默认与旧 ChiguoState 构造相同（15/55/40/85、tsundere 70）。

    注：旧 ChiguoState 构造情绪时 tsundere_index 不读 toml（恒为 dataclass 默认 70，
    toml 的 [emotion].tsundere_index 仅作人格回退键）；本函数允许该键覆盖，当前
    toml 未配置该键 → 默认路径与旧引擎逐位一致。构造即 clamp（域不变量）。
    """
    emo = _emotion_cfg(config)
    d = {
        "loneliness": _num(emo.get("loneliness", 15.0), 15.0),
        "affection": _num(emo.get("affection", 55.0), 55.0),
        "anxiety": _num(emo.get("anxiety", 40.0), 40.0),
        "energy": _num(emo.get("energy", 85.0), 85.0),
        "tsundere_index": _num(emo.get("tsundere_index", 70.0), 70.0),
    }
    _clamp_in_place(d)
    return AffectState(**d, user_mood=None)


def tick(
    state: AffectState,
    hours: float,
    now: datetime,
    *,
    silent_hours: float,
    config: dict,
    is_holiday: bool = False,
    is_school_day: bool = True,
    in_class: bool = False,
    class_load: str | None = None,
    tsundere_baseline: float | None = None,
    rng: random.Random | None = None,
) -> AffectState:
    """时间推进（旧 `state/emotion.py::EmotionMixin.tick` 全部语义）。

    now 仅作事件时间锚点（旧实现用于节假日/课表查询与跨日重置；情境已由显式
    参数注入，跨日重置属 cooldown 不在本域）。hours <= 0 / 非法 → 按 0 推进
    （同旧语义：弹性恢复恒等，但 A2 交互矩阵仍生效）。silent_hours 为 None/非法
    → 999.0（旧「从未交互」哨兵）。OU 噪声需跨 tick 复用同一 rng 实例才能与旧
    引擎的多 tick 序列一致；未提供时按 [emotion].noise_seed 自播种。
    """
    cfg = _emotion_cfg(config)
    h = _num(hours, 0.0)
    silent_h = _num(silent_hours, 999.0)
    elastic_base = _num(cfg.get("elastic_baseline", 100.0), 100.0)
    d = {name: getattr(state, name) for name in _AFFECT_FLOATS}
    old_lo, old_anx = d["loneliness"], d["anxiety"]

    # 1) loneliness：向 baseline 弹性恢复；静默 >24h 半衰期 ×0.6（_tick_loneliness）
    lo_hl = _num(cfg.get("loneliness_gain_half_life", 40.0), 40.0)
    if silent_h > 24:
        lo_hl *= 0.6
    d["loneliness"] = elastic_recover(old_lo, d["baseline_loneliness"], h, lo_hl, elastic_base)

    # 2) anxiety：节假日 ×2.5 / 非上课日 ×2.0 / 上课 ×1.8 / 课重 ×1.4（_tick_anxiety）
    anx_hl = _num(cfg.get("anxiety_gain_half_life", 30.0), 30.0)
    if is_holiday:
        anx_hl *= 2.5
    elif not is_school_day:
        anx_hl *= 2.0
    elif in_class:
        anx_hl *= 1.8
    elif class_load == "heavy":
        anx_hl *= 1.4
    d["anxiety"] = elastic_recover(old_anx, d["baseline_anxiety"], h, anx_hl, elastic_base)

    # 3) 变化率（仅 hours>0.01；取弹性恢复后、矩阵/噪声前的值，同旧实现）
    if h > 0.01:
        d["loneliness_rate"] = (d["loneliness"] - old_lo) / h
        d["anxiety_rate"] = (d["anxiety"] - old_anx) / h

    # 4) affection：静默 >24h 才向 baseline 极慢靠拢（_tick_affection）
    if silent_h > 24:
        ahl = _num(cfg.get("affection_loss_half_life", 500.0), 500.0)
        d["affection"] = elastic_recover(d["affection"], d["baseline_affection"], h,
                                         ahl, elastic_base)

    # 5) tsundere：高好感软化 / 高不安硬化 + 向人格基线回归（_tick_tsundere）
    if d["affection"] > 65:
        d["tsundere_index"] -= 0.3 * h
    if d["anxiety"] > 60:
        d["tsundere_index"] += 0.2 * h
    if tsundere_baseline is None:
        tb = _personality_cfg(config).get("tsundere_intensity", cfg.get("tsundere_index", 75.0))
    else:
        tb = tsundere_baseline
    tb = _num(tb, 75.0)
    if d["tsundere_index"] != tb:
        d["tsundere_index"] += (tb - d["tsundere_index"]) * (1 - 2.0 ** (-h / 200.0))

    # 6) energy：向 100 弹性恢复（_tick_energy）
    e_hl = _num(cfg.get("energy_regen_half_life", 8.0), 8.0)
    d["energy"] = elastic_recover(d["energy"], 100.0, h, e_hl, elastic_base)

    # 7) A2 交互矩阵（chiguo_math 纯函数，默认乘数 1.0 → 恒等）
    d.update(apply_interaction_matrix(d, cfg))

    # 8) OU 噪声（_tick_noise）：累积态显式随 state 传递；增量 semantics 同旧实现
    nx_lo, nx_anx = state.noise_loneliness, state.noise_anxiety
    if cfg.get("noise_enabled", 0) not in (0, False):
        theta = _num(cfg.get("noise_theta", 0.5), 0.5)
        lo_sigma = _num(cfg.get("noise_loneliness_sigma", 0.3), 0.3)
        anx_sigma = _num(cfg.get("noise_anxiety_sigma", 0.3), 0.3)
        noise_rng = rng if rng is not None else random.Random(
            int(_num(cfg.get("noise_seed", 42), 42.0)))
        lo_step = abs(d["loneliness"] - old_lo)
        anx_step = abs(d["anxiety"] - old_anx)
        prev_lo, prev_anx = nx_lo, nx_anx
        nx_lo = ou_step(prev_lo, 0.0, theta, lo_sigma, h, noise_rng)
        nx_anx = ou_step(prev_anx, 0.0, theta, anx_sigma, h, noise_rng)
        d["loneliness"] += noise_cap(lo_step, nx_lo - prev_lo)
        d["anxiety"] += noise_cap(anx_step, nx_anx - prev_anx)

    # 9) 基线淡忘（_tick_baseline_forget）：向全局默认半衰回归
    bf_hl = _num(cfg.get("baseline_forget_half_life", 720.0), 720.0)
    if bf_hl > 0 and h > 0:
        for dim, dflt in BASELINE_DEFAULTS.items():
            key = f"baseline_{dim}"
            cur = d[key]
            if cur != dflt:
                d[key] = cur + (dflt - cur) * (1 - 2.0 ** (-h / bf_hl))

    # 10) clamp（旧 _finalize 的情绪部分；跨日重置属 cooldown）
    _clamp_in_place(d)
    return replace(state, **d, noise_loneliness=nx_lo, noise_anxiety=nx_anx)


def _latency_multiplier(latency_hours: float, cfg: dict) -> dict:
    """回复速度倍率（旧 `state/mood.py::_latency_multiplier`）：
    秒回 → 好感 ×1.5 / 元气 +5 / 傲娇多降 2；正常 → 1.0；慢 → ×0.7；极慢 → ×0.4 + 不安回升。
    """
    fast = _num(cfg.get("reply_fast_threshold", 0.08), 0.08)
    slow = _num(cfg.get("reply_slow_threshold", 1.0), 1.0)
    very_slow = _num(cfg.get("reply_very_slow_threshold", 6.0), 6.0)
    if latency_hours <= fast:
        return {
            "affection": _num(cfg.get("reply_fast_affection_mult", 1.5), 1.5),
            "energy_extra": _num(cfg.get("reply_fast_energy_extra", 5.0), 5.0),
            "tsundere_extra_drop": _num(cfg.get("reply_fast_tsundere_extra", 2.0), 2.0),
            "anxiety_rebound": 0.0,
        }
    elif latency_hours <= slow:
        return {}  # 正常值
    elif latency_hours <= very_slow:
        return {
            "affection": _num(cfg.get("reply_slow_affection_mult", 0.7), 0.7),
            "energy_extra": 0.0,
            "tsundere_extra_drop": 0.0,
            "anxiety_rebound": 0.0,
        }
    else:
        return {
            "affection": _num(cfg.get("reply_very_slow_affection_mult", 0.4), 0.4),
            "energy_extra": 0.0,
            "tsundere_extra_drop": 0.0,
            "anxiety_rebound": _num(cfg.get("reply_very_slow_anxiety_rebound", 3.0), 3.0),
        }


def _latency_category(latency_hours: float | None) -> str:
    """旧 `_adapt_on_reply` 的延迟分档（阈值硬编码，与 _latency_multiplier 的配置阈值分离）。"""
    if latency_hours is None:
        return "normal"
    if latency_hours <= 0.08:
        return "fast"
    elif latency_hours <= 1.0:
        return "normal"
    elif latency_hours <= 6.0:
        return "slow"
    return "very_slow"


def _decay_pair(lo: float, anx: float, cfg: dict, damp: float) -> tuple[float, float]:
    """旧 `_decay_all`：回复骤降（半衰期内插），按 damp 缩放下降幅度。"""
    lo_hl = _num(cfg.get("loneliness_decay_on_reply", 0.35), 0.35)
    anx_hl = _num(cfg.get("anxiety_decay_on_reply", 0.5), 0.5)
    lo1 = lo + (decay(lo, 1.0, lo_hl) - lo) * damp
    anx1 = anx + (decay(anx, 1.0, anx_hl) - anx) * damp
    return lo1, anx1


def _affection_gain(msg_length: int, mult: float, damp: float, cfg: dict) -> float:
    g = _num(cfg.get("affection_gain_per_interaction", 0.8), 0.8)
    if msg_length > 30:
        g *= 1.5
    return g * mult * damp


def _energy_bonus(energy_extra: float, damp: float, cfg: dict) -> float:
    bonus = _num(cfg.get("energy_bonus_on_reply", 10.0), 10.0)
    return (bonus + energy_extra) * damp


def _tsundere_drop(extra: float, damp: float) -> float:
    return (1.5 + extra) * damp


def _damp(delta: float, channel: str, cfg: dict, affection: float) -> float:
    """旧 `MoodMixin._damp`：按通道效价选择惯性参数后走 impact_inertia。"""
    pos = _num(cfg.get("impact_inertia_positive", 0.0), 0.0)
    neg = _num(cfg.get("impact_inertia_negative", 0.0), 0.0)
    mod = _num(cfg.get("impact_inertia_affection_mod", 0.0), 0.0)
    if channel == "neg":
        return impact_inertia(delta, neg, neg, mod, affection)
    if channel == "pos":
        return impact_inertia(delta, pos, pos, mod, affection)
    return impact_inertia(delta, pos, neg, mod, affection)


def _normalize_event_type(event_type) -> str:
    """旧 `MoodMixin._normalize_event_type`：小写 + 去标点，保留中文/字母/数字/下划线。"""
    s = str(event_type or "").strip().lower()
    return re.sub(r"[^a-z0-9_一-鿿]", "", s)


def _extract_event_type(analysis: dict) -> str | None:
    """旧 `MoodMixin._extract_event_type`：显式键优先，缺省按 warmth/user_mood/topic 推断。"""
    if not isinstance(analysis, dict):
        return None
    for key in ("event_type", "event"):
        v = analysis.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    warmth = _num(analysis.get("warmth", 0.0), 0.0)
    mood = analysis.get("user_mood")
    if isinstance(mood, str) and mood.strip().lower() in ("low", "distressed"):
        return "comfort"
    if warmth > 0.3:
        return "praise"
    if warmth < -0.2:
        return "criticism"
    if analysis.get("topic"):
        return "new_topic"
    return None


def _consume_user_mood(user_mood: dict | None, analysis: dict,
                       now: datetime) -> dict | None:
    """旧 `MoodMixin._consume_user_mood` 的容错矩阵：
    缺键/非法枚举/非数值强度 → 保留旧感知；显式 calm 或强度 ≤0 → 清空；
    合法值 → 写入 {mood, intensity(钳制 [0,1]), at}。
    """
    if "user_mood" not in analysis:
        return user_mood
    try:
        mood = str(analysis.get("user_mood", "calm")).strip().lower()
    except (TypeError, ValueError):
        return user_mood
    if mood == "calm":
        return None
    if mood not in MOOD_DELTA:
        return user_mood
    intensity = _num(analysis.get("user_mood_intensity", 0.0), None)
    if intensity is None:
        return user_mood
    intensity = max(0.0, min(1.0, intensity))
    if intensity <= 0:
        return None
    return {"mood": mood, "intensity": intensity, "at": now.isoformat()}


def _apply_emotion_impact(d: dict, analysis: dict, cfg: dict, anx_before: float,
                          anxiety_sensitivity: float) -> None:
    """旧 `MoodMixin._apply_emotion_impact`：warmth/effort/attention 微调 + 不安敏感度缩放。

    注意顺序敏感：每一步的惯性阻尼都用“当时”的 affection（warmth 先加好感、
    effort 再读已更新的好感），与旧实现逐行一致。
    """
    def num(key: str, default: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, _num(analysis.get(key, default), default)))

    warmth = num("warmth", 0.0, -1.0, 1.0)
    effort = num("effort", 0.0, 0.0, 1.0)
    attention = num("attention", 0.0, 0.0, 1.0)

    # _impact_warmth
    d["affection"] += _damp(warmth * _num(cfg.get("affection_warmth_factor", 1.5), 1.5),
                            "auto", cfg, d["affection"])
    d["energy"] += _damp(warmth * _num(cfg.get("energy_warmth_factor", 4.0), 4.0),
                         "auto", cfg, d["affection"])
    if warmth < 0:
        d["anxiety"] += _damp(abs(warmth) * _num(cfg.get("anxiety_warmth_recovery", 3.0), 3.0),
                              "neg", cfg, d["affection"])

    # _impact_effort
    d["affection"] += _damp(effort * _num(cfg.get("affection_effort_factor", 1.0), 1.0),
                            "auto", cfg, d["affection"])
    d["tsundere_index"] -= _damp(effort * _num(cfg.get("tsundere_effort_factor", 2.0), 2.0),
                                 "pos", cfg, d["affection"])

    # _impact_attention
    d["energy"] += _damp(attention * _num(cfg.get("energy_attention_factor", 4.0), 4.0),
                         "auto", cfg, d["affection"])
    if attention < 0.3:
        d["anxiety"] += _damp((0.3 - attention) * _num(cfg.get("anxiety_ignore_factor", 2.0), 2.0),
                              "neg", cfg, d["affection"])

    # _impact_anxiety_sens：只缩放 analysis 引入的不安 delta（事件 delta 不参与）
    if anxiety_sensitivity != 1.0:
        diff = d["anxiety"] - anx_before
        if diff != 0:
            d["anxiety"] = anx_before + diff * anxiety_sensitivity


def _apply_baseline_drift(d: dict, interaction: dict, cfg: dict) -> None:
    """旧 `update_emotion_baseline`：只漂移 loneliness/anxiety/affection 三个基线维度，
    有界钳制 [全局默认 ± baseline_max_drift]；baseline_drift_rate ≤0 → 恒等。"""
    rate = _num(cfg.get("baseline_drift_rate", 0.0), 0.0)
    if rate <= 0:
        return
    max_drift = _num(cfg.get("baseline_max_drift", 20.0), 20.0)
    for dim, delta in baseline_shift_of(interaction).items():
        if delta == 0:
            continue
        step = _num(cfg.get(f"baseline_shift_{dim}", 0.15), 0.15)
        key = f"baseline_{dim}"
        cur = d[key] + delta * rate * step
        dflt = BASELINE_DEFAULTS[dim]
        d[key] = max(dflt - max_drift, min(dflt + max_drift, cur))


def _apply_analysis_impact(d: dict, user_mood: dict | None, analysis: dict,
                          now: datetime, config: dict, cfg: dict,
                          anxiety_sensitivity: float) -> dict | None:
    """旧 `MoodMixin._apply_analysis_impact` 的数值部分（pending 话题摄入不在本域）。

    顺序：B1 事件 delta（直接加减 + clamp）→ 记录 analysis 前不安 → warmth/effort/
    attention 微调 → 消费 user_mood → TTL 内重放 user_mood delta → clamp。
    """
    if cfg.get("event_delta_enabled", False):
        event_type = _extract_event_type(analysis)
        if event_type:
            key = _normalize_event_type(event_type)
            key = EVENT_TYPE_SYNONYMS.get(key, key)
            delta = EVENT_DELTA.get(key)
            if delta:
                for dim, dv in delta.items():
                    if dim in d:
                        d[dim] += dv
                _clamp_in_place(d)

    anx_before = d["anxiety"]
    _apply_emotion_impact(d, analysis, cfg, anx_before, anxiety_sensitivity)

    user_mood = _consume_user_mood(user_mood, analysis, now)
    ttl = _num(_trigger_cfg(config).get("user_mood_ttl_minutes", 360.0), 360.0)
    if mood_fresh(user_mood, now, ttl):
        intensity = _num((user_mood or {}).get("intensity", 0.0), 0.0)
        for dim, dv in user_mood_impact(user_mood.get("mood", "calm"), intensity, cfg).items():
            if dim in d:
                d[dim] += dv

    _clamp_in_place(d)
    return user_mood


def apply_user_message(
    state: AffectState,
    now: datetime,
    *,
    msg_length: int = 10,
    latency_hours: float | None = None,
    analysis: dict | None = None,
    config: dict,
    damp: float = 1.0,
    anxiety_sensitivity: float = 1.0,
) -> AffectState:
    """用户消息情绪结算（旧 `on_user_message` + `_apply_analysis_impact` 的数值语义）。

    - `damp` = 旧 `_reply_damp` 的 A10 饱和阻尼系数（reducer 按 drop_events 窗口算好传入）；
    - `latency_hours` = 距上次主动发送的小时数（旧 `_compute_latency`；reducer 从消息
      历史计算）。None → 无倍率（旧无 last_message_at 语义）；
    - `anxiety_sensitivity` = 旧 `personality.anxiety_sensitivity()`（neuroticism 派生），
      默认 1.0 恒等；
    - 非法输入回退：msg_length/damp → 默认；latency  NaN → 无倍率；analysis 非 dict → 忽略。
    非情绪职责（cooldown 时间戳/未回复计数/drop_events 记录/λ 重置/人格演化/生物钟/
    Hawkes 事件）不在此实现，由 reducer 层承担。
    """
    cfg = _emotion_cfg(config)
    if now is None:
        now = datetime.now(CST)
    if analysis is not None and not isinstance(analysis, dict):
        analysis = None
    msg_len = int(_num(msg_length, 10.0))
    damp_v = _num(damp, 1.0)
    lat = _num(latency_hours, None) if latency_hours is not None else None
    lat_mult = _latency_multiplier(lat, cfg) if lat is not None else {}

    d = {name: getattr(state, name) for name in _AFFECT_FLOATS}
    user_mood = state.user_mood if isinstance(state.user_mood, dict) else None

    # 1) 回复骤降 + 延迟倍率 + 好感/元气/傲娇（旧 on_user_message 前六步）
    d["loneliness"], d["anxiety"] = _decay_pair(d["loneliness"], d["anxiety"], cfg, damp_v)
    d["anxiety"] += _num(lat_mult.get("anxiety_rebound", 0.0), 0.0)
    d["affection"] += _affection_gain(msg_len, _num(lat_mult.get("affection", 1.0), 1.0),
                                      damp_v, cfg)
    d["energy"] += _energy_bonus(_num(lat_mult.get("energy_extra", 0.0), 0.0), damp_v, cfg)
    d["tsundere_index"] -= _tsundere_drop(_num(lat_mult.get("tsundere_extra_drop", 0.0), 0.0),
                                          damp_v)

    # 2) analysis 微调（含 EVENT_DELTA / user_mood / 不安敏感度）
    if analysis is not None:
        user_mood = _apply_analysis_impact(d, user_mood, analysis, now, config, cfg,
                                           _num(anxiety_sensitivity, 1.0))

    # 3) 事件驱动基线漂移（旧 _adapt_on_reply → update_emotion_baseline；
    #    adapt_personality 属人格域，不在本域）
    _apply_baseline_drift(d, {
        "type": "user_reply",
        "warmth": _num(analysis.get("warmth", 0.0), 0.0) if analysis else 0.0,
        "latency_category": _latency_category(lat),
        "msg_length": msg_len,
    }, cfg)

    # 4) clamp（旧 on_user_message 末尾 _finalize 的情绪部分）
    _clamp_in_place(d)
    return replace(state, **d, user_mood=user_mood)


def apply_character_send(state: AffectState, *, config: dict) -> AffectState:
    """迟菓发出主动消息的情绪记账（旧 `on_character_message` 数值部分）：
    energy - cost（下限 0）、loneliness 半衰衰减、anxiety + gain，末尾 clamp。
    日计数/Hawkes 事件/崩溃记录/λ 归零等 cooldown 职责由 reducer 承担。
    """
    cfg = _emotion_cfg(config)
    cost = _num(cfg.get("energy_cost_per_message", 20.0), 20.0)
    send_hl = _num(cfg.get("loneliness_decay_on_send", 2.0), 2.0)
    anx_gain = _num(cfg.get("anxiety_gain_on_send", 2.0), 2.0)
    d = {
        "energy": max(0.0, state.energy - cost),
        "loneliness": decay(state.loneliness, 1.0, send_hl),
        "anxiety": state.anxiety + anx_gain,
    }
    _clamp_in_place(d)
    return replace(state, **d)


def refund_send(state: AffectState, *, config: dict) -> AffectState:
    """发送失败退款的情绪数值回滚（旧 `refund_send` 数值部分）：
    energy 回补（封顶 100）、anxiety 回减（下限 0），末尾 clamp。
    事件定位（msg_id/legacy 回退）与 FIFO 去重属事件层，由调用方在 reducer 决定
    是否执行本回滚——本函数只表达“回滚本身”。
    """
    cfg = _emotion_cfg(config)
    cost = _num(cfg.get("energy_cost_per_message", 20.0), 20.0)
    anx_gain = _num(cfg.get("anxiety_gain_on_send", 2.0), 2.0)
    d = {
        "energy": min(100.0, state.energy + cost),
        "anxiety": max(0.0, state.anxiety - anx_gain),
    }
    _clamp_in_place(d)
    return replace(state, **d)
