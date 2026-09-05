# ============================================================
# chiguo_trigger.py — 触发器引擎 v2
# 用 sigmoid 概率加权随机选择，替代硬阈值排序
# ============================================================

import logging
import random
import math
import sys
from datetime import datetime
from dataclasses import dataclass, field

from chiguo_state import CST, ChiguoState
from chiguo_math import cfg_float, clamp01, clamp_int, weighted_trigger_choice, in_quiet_window, mood_fresh

from trigger_types import TriggerType, EMOTION_TRIGGERS, RITUAL_TRIGGERS

from chiguo_concurrent import TIMEOUT, call_with_timeout

# #401：trigger 侧 mem0 读调用超时预算（秒）。random_memory / user_relevant 是
# evaluate 锁内唯一的无上限网络 I/O（ollama 挂起可致锁内无限阻塞，持锁超 5s
# 触发 flock 降级阈值）；超时放弃等待走空候选 + 审计降级（与 mem0_backend 读侧
# _MEM0_TIMEOUT=10s 同预算）。
_TRIGGER_MEM0_TIMEOUT = 10.0

# ── T08: jitter 隔离实例（不污染全局 random）──────────────────────
_jitter_rng = random.Random()
# 别名供测试探针（兼容 _rng / jitter_rng 命名）
_jitter_rng_alias = _jitter_rng
_rng = _jitter_rng
jitter_rng = _jitter_rng


@dataclass
class Trigger:
    type: str
    intensity: str = "soft"
    data: dict = field(default_factory=dict)


def _clamp01(value, default: float) -> float:
    """薄包装：委托 chiguo_math.clamp01（单源，PR-4 AUD-026 收敛）。"""
    return clamp01(value, default)


def _clamp_int(value, default: int, max_value: int | None = None) -> int:
    """薄包装：委托 chiguo_math.clamp_int（单源，PR-4 AUD-026 收敛）。"""
    return clamp_int(value, default, max_value)


def backoff_level(state: ChiguoState, now: datetime) -> int:
    """
    A5 未回复退场状态机（三态，不新增持久化字段——用现有 messages_without_reply 推导）：
      0 = normal      （< backoff_start 条未回复：正常竞争）
      1 = backing_off （backoff_start ≤ n < backoff_silent：情绪类禁发、仪式类照发）
      2 = silent      （n ≥ backoff_silent：全禁发；escape_valve longing 破防豁免——防死锁语义；
                        D4 #349：reminder 一次性记忆豁免——用户显式托付准时优先，类比 escape 豁免族）
    参数：[cooldown].backoff_start=3 / backoff_silent=5。
    与 current_lambda() 的 0.7^n 退避（λ 降频）是两层独立机制：λ 降频 + 硬性禁发层，不冲突。
    now 保留作签名（未来可用 last_user_message_at 加时间窗扩展），当前只用计数推导。
    """
    cfg = state.config.get("cooldown", {})
    # #83: 类型防护——配置为 "3.5"/None 等非整数时回退默认，防 ValueError/TypeError 崩溃
    start = _clamp_int(cfg.get("backoff_start", 3), 3, max_value=100)
    silent = _clamp_int(cfg.get("backoff_silent", 5), 5, max_value=100)
    n = state.cooldown.get_messages_without_reply()
    if n >= silent:
        return 2
    if n >= start:
        return 1
    return 0


def _due_reminder_trigger(state: ChiguoState, now: datetime, trg_cfg: dict) -> dict | None:
    """D4 (#349)：收集并随机选一个「窗口内到触发」的 reminder 一次性记忆候选。

    「提醒准时优先」用户决策（R9 F-A5-01）：reminder 是显式托付的一次性记忆，
    必须准点发出。窗口/去重判定与主记忆循环共用 `_memory_should_trigger`（单一事实源）——
    不重复造判定，reminder 分支不设 must_send 标记（见 R9），门禁层经本函数在
    silent 态提前发现 reminder 并豁免（类似 escape_valve 在退场首部豁免）。
    多个 reminder 同时到点时按相等权重随机选一（weight 仅参与排序，实际都=1）。
    返回候选 dict（含 trigger），无到点 reminder 时返回 None。
    """
    cands = []
    for mem in state.memories:
        if not isinstance(mem, dict):
            continue
        if mem.get("type") == "reminder" \
                and _memory_should_trigger(mem, now, trg_cfg):
            cands.append({
                "trigger": Trigger(type=TriggerType.MEMORY, intensity="soft",
                                   data={"memory": mem}),
                "weight": 1.0,
            })
    if not cands:
        return None
    chosen = weighted_trigger_choice(cands)
    return chosen if chosen is not None else None



def _collect_ritual_candidates(state, now, trg_cfg, ritual_scale) -> list[dict]:
    """仪式类候选收集：特殊日/早安/晚安/用餐/手动记忆/mem0 随机浮现"""
    cands: list[dict] = []
    ritual_special = cfg_float(trg_cfg.get("ritual_special_weight", 3.0), 3.0, clamp_min=0.0)
    ritual_morning = cfg_float(trg_cfg.get("ritual_morning_weight", 2.5), 2.5, clamp_min=0.0)
    ritual_night = cfg_float(trg_cfg.get("ritual_night_weight", 2.0), 2.0, clamp_min=0.0)
    ritual_meal = cfg_float(trg_cfg.get("ritual_meal_weight", 0.8), 0.8, clamp_min=0.0)
    ritual_memory = cfg_float(trg_cfg.get("ritual_memory_weight", 2.0), 2.0, clamp_min=0.0)
    ritual_mem0 = cfg_float(trg_cfg.get("ritual_mem0_weight", 1.5), 1.5, clamp_min=0.0)
    mem0_min_silent = cfg_float(trg_cfg.get("mem0_surface_min_silent_hours", 6.0), 6.0, clamp_min=0.0)
    mem0_prob = _clamp01(trg_cfg.get("mem0_surface_probability", 0.08), 0.08)
    # 特殊日期
    try:
        anniv_today = state.anniversary_mgr.get_today(now.date())
        special_hit = any(a.type == "anniversary" for a in anniv_today)
    except (ValueError, TypeError, OSError):
        special_hit = False
    if special_hit:
        cands.append({"trigger": Trigger(type=TriggerType.SPECIAL, intensity="soft"), "weight": ritual_special * ritual_scale})
    if _should_morning(state, now):
        cands.append({"trigger": Trigger(type=TriggerType.MORNING, intensity="soft"), "weight": ritual_morning * ritual_scale})
    if _should_night(state, now):
        cands.append({"trigger": Trigger(type=TriggerType.NIGHT, intensity="soft"), "weight": ritual_night * ritual_scale})
    if _should_meal(now, state):
        cands.append({"trigger": Trigger(type=TriggerType.MEAL, intensity="soft"), "weight": ritual_meal * ritual_scale})
    for mem in state.memories:
        if not isinstance(mem, dict):
            continue
        if _memory_should_trigger(mem, now, trg_cfg):
            cands.append({"trigger": Trigger(type=TriggerType.MEMORY, intensity="soft", data={"memory": mem}), "weight": ritual_memory * ritual_scale})
    silent_h = state.cooldown.silent_hours(now)
    if silent_h > mem0_min_silent and random.random() < mem0_prob and state.memory_bridge.available:
        # #401：锁内 mem0 读加 10s 超时兜底；超时 → 空候选 + 审计降级。
        mem0_mem = call_with_timeout(
            lambda: state.memory_bridge.random_memory(min_importance=0.4),
            _TRIGGER_MEM0_TIMEOUT, name="trigger-random-memory")
        if mem0_mem is TIMEOUT:
            state.audit("trigger_mem0_timeout", "random_memory 10s 超时，走空候选降级")
            mem0_mem = None
        if mem0_mem:
            cands.append({"trigger": Trigger(type=TriggerType.MEMORY, intensity="soft", data={"mem0_memory": mem0_mem}), "weight": ritual_mem0 * ritual_scale})
    return cands


def _collect_followup_candidates(state, now, trg_cfg) -> list[dict]:
    """接话茬候选收集：pending 话题 + 记忆兜底"""
    cands: list[dict] = []
    fup_min = cfg_float(trg_cfg.get("follow_up_min_age_hours", 2.0), 2.0, clamp_min=0.0)
    fup_max = cfg_float(trg_cfg.get("follow_up_max_age_hours", 48.0), 48.0, clamp_min=0.0)
    state.prune_pending_topics(now, fup_max)
    follow_entries: list[tuple[dict, float]] = []
    for t in state.pending_topics:
        try:
            dt = datetime.fromisoformat(t.get("created_at", ""))
        except (ValueError, TypeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=CST)
        age = (now - dt).total_seconds() / 3600
        if fup_min <= age <= fup_max:
            follow_entries.append((t, age))
    if follow_entries:
        for entry, age in follow_entries:
            cand = _followup_candidate(entry, age, trg_cfg)
            if cand:
                cands.append(cand)
    elif (not state.pending_topics and state.memory_bridge.available
          and random.random() < _clamp01(trg_cfg.get("followup_memory_probability", 0.5), 0.5)):
        # #401：锁内 mem0 读加 10s 超时兜底；超时 → 空候选 + 审计降级。
        mems = call_with_timeout(
            lambda: list(state.memory_bridge.user_relevant(limit=10, min_importance=0.4)),
            _TRIGGER_MEM0_TIMEOUT, name="trigger-user-relevant")
        if mems is TIMEOUT:
            state.audit("trigger_mem0_timeout", "user_relevant 10s 超时，走空候选降级")
            mems = []
        now_ts = now.timestamp()
        for mem in mems:
            ts = mem.get("timestamp") or 0
            if not ts or not isinstance(ts, (int, float)):
                continue
            ts = ts / 1000.0 if ts > 1e12 else ts
            age = (now_ts - ts) / 3600
            if 0 < age <= fup_max:
                text = (mem.get("text") or mem.get("l0_abstract") or "").strip()[:50]
                if text:
                    follow_entries.append(({"topic": text, "source": "memory", "created_at": now.isoformat()}, age))
                    break
        if follow_entries:
            cand = _followup_candidate(follow_entries[0][0], follow_entries[0][1], trg_cfg)
            if cand:
                cands.append(cand)
    return cands


def _collect_emotion_candidates(state, now, trg_cfg, silent_h) -> list[dict]:
    """情绪驱动候选收集：孤独三级/anxiety/comfort/boredom/reflect/longing
    T08: rate/tsundere/bias/w阈 均走 CONFIG（cfg_float/_clamp01），默认值=原硬编码，行为恒等。"""
    cands: list[dict] = []
    emo = state.emotion
    tsun = emo.tsundere_index / 100
    lo_rate = emo.loneliness_rate
    anx_rate = emo.anxiety_rate
    # T08 CONFIG 单源：rate_factor 阈值与系数
    lo_thresh = cfg_float(trg_cfg.get("lonely_rate_lo_threshold", 1.5), 1.5, clamp_min=0.0)
    lo_factor = cfg_float(trg_cfg.get("lonely_rate_lo_factor", 0.3), 0.3, clamp_min=0.0)
    anx_thresh = cfg_float(trg_cfg.get("lonely_rate_anx_threshold", 2.0), 2.0, clamp_min=0.0)
    anx_factor = cfg_float(trg_cfg.get("lonely_rate_anx_factor", 0.2), 0.2, clamp_min=0.0)
    rate_factor = 1.0 + max(0, (lo_rate - lo_thresh) * lo_factor) + max(0, (anx_rate - anx_thresh) * anx_factor)
    # T08 CONFIG 单源：tsundere 修饰
    tsun_low = cfg_float(trg_cfg.get("lonely_tsundere_low_factor", 0.3), 0.3, clamp_min=0.0)
    tsun_mid = cfg_float(trg_cfg.get("lonely_tsundere_mid_factor", 0.5), 0.5, clamp_min=0.0)
    tsun_high = cfg_float(trg_cfg.get("lonely_tsundere_high_factor", 0.4), 0.4, clamp_min=0.0)
    lonely_bias = cfg_float(trg_cfg.get("lonely_bias", 0.5), 0.5, clamp_min=0.0)
    raw_low = state.trigger_weight("lonely_low") * (1 + tsun_low * tsun) * rate_factor
    raw_mid = state.trigger_weight("lonely_mid") * (1 + tsun_mid * tsun) * rate_factor
    raw_high = state.trigger_weight("lonely_high") * (1 - tsun_high * tsun) * rate_factor
    total = raw_low + raw_mid + raw_high + lonely_bias
    w_low = raw_low / total
    w_mid = raw_mid / total
    w_high = raw_high / total
    w_low_thr = _clamp01(trg_cfg.get("lonely_w_low_threshold", 0.03), 0.03)
    w_mid_thr = _clamp01(trg_cfg.get("lonely_w_mid_threshold", 0.03), 0.03)
    w_high_thr = _clamp01(trg_cfg.get("lonely_w_high_threshold", 0.02), 0.02)
    if w_low > w_low_thr:
        cands.append({"trigger": Trigger(type=TriggerType.LONELY_LOW, intensity="soft"), "weight": w_low})
    if w_mid > w_mid_thr:
        cands.append({"trigger": Trigger(type=TriggerType.LONELY_MID, intensity="medium"), "weight": w_mid})
    if w_high > w_high_thr:
        cands.append({"trigger": Trigger(type=TriggerType.LONELY_HIGH, intensity="intense"), "weight": w_high})
    # anxiety
    anx_baseline = _clamp01(trg_cfg.get("anxiety_baseline", 0.5), 0.5)
    anx_min_weight = _clamp01(trg_cfg.get("anxiety_min_weight", 0.3), 0.3)
    raw_anx = state.trigger_weight("anxiety")
    mood = state.cooldown.get_user_mood()
    mood_fresh_flag = bool(mood and mood_fresh(mood, now, trg_cfg.get("user_mood_ttl_minutes", 360.0)))
    if mood_fresh_flag:
        anx_bonus = cfg_float(trg_cfg.get("user_mood_anxiety_bonus", 0.0), 0.0, clamp_min=0.0)
        if anx_bonus > 0 and mood.get("mood") in ("low", "distressed"):
            raw_anx = raw_anx * (1.0 + anx_bonus * mood.get("intensity", 0.0))
    raw_anx = min(1.0, raw_anx)
    denom_anx = raw_anx + anx_baseline * (1 - raw_anx)
    w_anx = raw_anx / denom_anx if denom_anx > 0 else 0.0
    if w_anx > anx_min_weight:
        cands.append({"trigger": Trigger(type=TriggerType.ANXIETY, intensity="medium"), "weight": w_anx})
    # comfort
    comfort_base = cfg_float(trg_cfg.get("comfort_weight_base", 0.0), 0.0, clamp_min=0.0)
    if mood_fresh_flag and comfort_base > 0 and mood.get("mood") in ("low", "distressed"):
        raw_cf = comfort_base * mood.get("intensity", 0.0) * (1 + (emo.affection - 50) / 100)
        cf_baseline = _clamp01(trg_cfg.get("comfort_baseline", 0.5), 0.5)
        cf_min = _clamp01(trg_cfg.get("comfort_min_weight", 0.03), 0.03)
        w_cf = raw_cf / (raw_cf + cf_baseline) if raw_cf + cf_baseline > 0 else 0.0
        if w_cf > cf_min:
            cands.append({"trigger": Trigger(TriggerType.COMFORT, "soft"), "weight": w_cf})
    aff_factor = 1 + (emo.affection - 50) / 100
    if emo.energy > 70 and 2 < silent_h < 48 and _is_free_time(state, now):
        pers_extra = getattr(getattr(state, 'personality', None), 'extraversion', 60.0)
        pers_extra_factor = 0.5 + (pers_extra / 100) * 1.0
        w_bored = (cfg_float(trg_cfg.get("playful_base_weight", 0.15), 0.15, clamp_min=0.0) * (emo.energy / 100) * aff_factor * pers_extra_factor)
        if w_bored > 0.03:
            cands.append({"trigger": Trigger(type=TriggerType.PLAYFUL, intensity="soft"), "weight": w_bored})
    pers = getattr(state, 'personality', None)
    if pers:
        neuroticism = getattr(pers, 'neuroticism', 60.0)
        if (emo.affection > 70 and silent_h < 2 and emo.energy > 60 and neuroticism < 70 and random.random() < _clamp01(trg_cfg.get("reflect_probability", 0.08), 0.08)):  # fallback: CONFIG
            w_reflect = cfg_float(trg_cfg.get("reflect_base_weight", 0.08), 0.08, clamp_min=0.0) * (emo.affection / 100) * (1 - neuroticism / 100) * (emo.energy / 100)  # fallback: CONFIG
            if w_reflect > 0.02:
                cands.append({"trigger": Trigger(type=TriggerType.REFLECT, intensity="soft"), "weight": w_reflect})
    held = state.cooldown.get_held_count()
    acc_lam = state.cooldown.get_accumulated_lambda() or 0
    base_lambda = cfg_float(state.config.get("poisson", {}).get("base_lambda", 0.25), 0.25, clamp_min=0.0)
    if state.is_longing_overflow() and base_lambda > 0:
        longing_cap = cfg_float(trg_cfg.get("longing_cap", 0.5), 0.5, clamp_min=0.0)
        longing_factor = cfg_float(trg_cfg.get("longing_factor", 0.3), 0.3, clamp_min=0.0)
        longing_min = _clamp01(trg_cfg.get("longing_min_weight", 0.03), 0.03)
        w_longing = min(longing_cap, (acc_lam / base_lambda - 1) * longing_factor)
        if w_longing > longing_min:
            cands.append({"trigger": Trigger(type=TriggerType.LONGING, intensity="soft", data={"held_count": held, "accumulated_lambda": round(acc_lam, 3)}), "weight": w_longing})
    return cands


def _apply_modifiers_and_select(state, now, trg_cfg, weighted_candidates, trigger_scale):
    """应用 A3/A6/A4/抖动/反馈并做三段选择"""
    if trigger_scale:
        for c in weighted_candidates:
            c["weight"] *= trigger_scale.get(c["trigger"].type, trigger_scale.get("default", 1.0))
    free_mult = cfg_float(trg_cfg.get("free_multiplier", 1.2), 1.2, clamp_min=0.0)
    sched_mult = _schedule_multiplier(state, now, free_mult)
    for c in weighted_candidates:
        if c["trigger"].type in EMOTION_TRIGGERS:
            c["weight"] *= sched_mult
    repeat_decay = _clamp01(trg_cfg.get("repeat_decay", 0.6), 0.6)
    repeat_cap = _clamp_int(trg_cfg.get("repeat_cap", 3), 3)
    history = state.cooldown.get_trigger_history()
    for c in weighted_candidates:
        n = sum(1 for t in history if t == c["trigger"].type)
        c["weight"] *= repeat_decay ** min(n, repeat_cap)
    min_activation = _clamp01(trg_cfg.get("min_activation", 0.08), 0.08)
    must_send_activation = _clamp01(trg_cfg.get("must_send_activation", 0.75), 0.75)
    if min_activation >= must_send_activation:
        print(f"[trigger] WARNING: min_activation({min_activation:.2f}) >= must_send_activation({must_send_activation:.2f})", file=sys.stderr)
    emo_cands = [c for c in weighted_candidates if c["trigger"].type in EMOTION_TRIGGERS]
    ritual_cands = [c for c in weighted_candidates if c["trigger"].type in RITUAL_TRIGGERS]
    activation = _activation_score(emo_cands)
    must_send = False
    # T08: jitter 区间走 CONFIG + 隔离 Random（不污染全局 random）
    jitter_low = cfg_float(trg_cfg.get("jitter_low", 0.8), 0.8, clamp_min=0.0)
    jitter_high = cfg_float(trg_cfg.get("jitter_high", 1.2), 1.2, clamp_min=0.0)
    # 防御：非数值/反序/相等时回退 0.8/1.2
    if not math.isfinite(jitter_low) or not math.isfinite(jitter_high) or jitter_low >= jitter_high:
        jitter_low, jitter_high = 0.8, 1.2
    # T08 隔离：用全局状态快照播种隔离实例 → 确定性跟随全局 seed 但不污染全局序列
    try:
        _jitter_rng.setstate(random.getstate())
    except (ValueError, TypeError, OSError):
        pass
    jitter = _jitter_rng.uniform(jitter_low, jitter_high)
    for c in weighted_candidates:
        if c["trigger"].type in EMOTION_TRIGGERS:
            c["weight"] *= jitter
    if trg_cfg.get("reply_feedback_enabled", 0):
        stats = state.cooldown.get_reply_stats() or {}
        rfb_damp = cfg_float(trg_cfg.get("reply_feedback_damp", 0.0), 0.0, clamp_min=0.0)
        rfb_boost = cfg_float(trg_cfg.get("reply_feedback_boost", 0.0), 0.0, clamp_min=0.0)
        rfb_low = cfg_float(trg_cfg.get("reply_feedback_low_rate", 0.3), 0.3, clamp_min=0.0)
        rfb_high = cfg_float(trg_cfg.get("reply_feedback_high_rate", 0.7), 0.7, clamp_min=0.0)
        rfb_min = _clamp_int(trg_cfg.get("reply_feedback_min_samples", 3), 3)
        for c in weighted_candidates:
            st = stats.get(c["trigger"].type) or {}
            try:
                sent, replied = int(st.get("sent", 0)), int(st.get("replied", 0))
            except (TypeError, ValueError):
                continue
            if sent < rfb_min or sent <= 0:
                continue
            rate = replied / sent
            if rate < rfb_low:
                c["weight"] *= max(0.0, 1.0 - rfb_damp)
            elif rate >= rfb_high:
                c["weight"] *= max(0.0, 1.0 + rfb_boost)
    reminder_cands = [c for c in weighted_candidates if c["trigger"].type == TriggerType.MEMORY and c["trigger"].data.get("memory", {}).get("type") == "reminder"]
    if reminder_cands:
        chosen = weighted_trigger_choice(reminder_cands)
    elif activation >= must_send_activation and emo_cands:
        chosen = weighted_trigger_choice(emo_cands)
        must_send = True
    elif activation < min_activation:
        chosen = weighted_trigger_choice(ritual_cands)
    else:
        chosen = weighted_trigger_choice(weighted_candidates)
    return chosen, must_send, activation


def evaluate_triggers(state: ChiguoState, now: datetime,
                      trigger_scale: dict | None = None) -> Trigger | None:
    """评估触发 — 表驱动重构：主函数仅编排，候选收集下沉到 _collect_* 助手。"""
    if state.longing_break_eligible(now):
        return Trigger(TriggerType.LONGING, "high", data={"escape_valve": True})
    trg_cfg = state.config.get("trigger", {})
    backoff = backoff_level(state, now)
    if backoff >= 2:
        reminder = _due_reminder_trigger(state, now, trg_cfg)
        if reminder is not None:
            return reminder["trigger"]
        return None
    weighted_candidates: list[dict] = []
    ritual_scale = cfg_float(state.config.get("cooldown", {}).get("ritual_weight_scale", 1.0), 1.0, clamp_min=0.0)
    weighted_candidates.extend(_collect_ritual_candidates(state, now, trg_cfg, ritual_scale))
    weighted_candidates.extend(_collect_followup_candidates(state, now, trg_cfg))
    silent_h = state.cooldown.silent_hours(now)
    weighted_candidates.extend(_collect_emotion_candidates(state, now, trg_cfg, silent_h))
    if backoff == 1:
        weighted_candidates = [c for c in weighted_candidates if c["trigger"].type in RITUAL_TRIGGERS]
    if not weighted_candidates:
        return None
    chosen, must_send, _ = _apply_modifiers_and_select(state, now, trg_cfg, weighted_candidates, trigger_scale)
    if chosen is None:
        return None
    trigger = chosen["trigger"]
    if must_send:
        trigger.data["must_send"] = True
    if trigger.type == TriggerType.FOLLOW_UP and chosen.get("topic_ref") is not None:
        state.mark_pending_topic_attempted(chosen["topic_ref"].get("topic", ""))
    safety = state.safety_level(now)
    if safety >= 1 and trigger.type == TriggerType.LONELY_HIGH:
        trigger = Trigger(type=TriggerType.LONELY_MID, intensity="soft", data=trigger.data)
    elif safety >= 2:
        if trigger.type == TriggerType.ANXIETY:
            trigger = Trigger(type=TriggerType.LONELY_LOW, intensity="soft", data=trigger.data)
        else:
            trigger.intensity = "soft"
    return trigger


def _schedule_multiplier(state: ChiguoState, now: datetime, free_mult: float) -> float:
    """A3 日程乘数：上课中 0.3 / 空闲 free_multiplier（默认 1.2）/ 半忙 0.6。
    只经 chiguo_state 既有只读接口判断（schedule_status/_is_free_time），不直触 schedule 包。
    schedule_status 异常/课表不可用 → None → 按空闲处理（与 _is_free_time 同语义）。"""
    try:
        sch = state.schedule_status(now)
        if sch and sch.get("in_class"):
            return 0.3
    except (ValueError, TypeError, OSError):
        logging.debug("free_mult 计算失败: %s", __import__('traceback').format_exc(), exc_info=False)
    if _is_free_time(state, now):
        return free_mult
    return 0.6


def _activation_score(emo_cands: list[dict]) -> float:
    """A4 activation：按情绪维度族取 max（#79 后 CONFIG fallback must_send_activation=0.75 按单源标定）。

    孤独三级（lonely_low/mid/high）是同一孤独维度的互斥表达 → 族内求和（孤独总量压力）；
    其余情绪（anxiety/playful/reflect/longing/comfort）各自独立维度 → 单源取 max。
    与全量求和的差异：两股中低情绪叠加（如孤独35+焦虑57）不再凑到高段触发 must_send，
    只有单个维度真正强（孤独族和或单源焦虑 ≥ 阈值）才必发 —— 与 toml #79 文档承诺一致。"""
    lonely = sum(c["weight"] for c in emo_cands
                 if c["trigger"].type in
                 (TriggerType.LONELY_LOW, TriggerType.LONELY_MID, TriggerType.LONELY_HIGH))
    others = [c["weight"] for c in emo_cands
              if c["trigger"].type not in
              (TriggerType.LONELY_LOW, TriggerType.LONELY_MID, TriggerType.LONELY_HIGH)]
    return max(lonely, max(others, default=0.0))


def _followup_candidate(entry: dict, age: float, trg_cfg: dict) -> dict | None:
    """follow_up 候选组装（主块与记忆兜底块共用）。权重 = weight × 年龄钟形。
    #79: entry 缺 topic 键/非字符串/空白 → 跳过（防 KeyError 与空话题）。"""
    topic = entry.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return None
    # 峰值/钟形宽度配置防御：非数值回退默认；sigma<=0 → 回退 3.0（bell 分母
    # e^{-((age-peak)/sigma)^2}，sigma=0 会 ZeroDivisionError，兄弟配置键同有 clamp 防御）。
    peak = cfg_float(trg_cfg.get("follow_up_peak_hours", 4.0), 4.0)
    sigma = cfg_float(trg_cfg.get("follow_up_sigma_hours", 3.0), 3.0)
    if not math.isfinite(peak) or not math.isfinite(sigma) or sigma <= 0:
        peak, sigma = 4.0, 3.0
    # sigma 已经 cfg_float 守卫 nan/inf，仍需钳制 <=0 回退默认，已在上方处理
    bell = math.exp(-((age - peak) / sigma) ** 2)
    w = cfg_float(trg_cfg.get("follow_up_weight", 0.35), 0.35, clamp_min=0.0) * bell
    if w <= _clamp01(trg_cfg.get("follow_up_min_weight", 0.03), 0.03):
        return None
    return {
        "trigger": Trigger(type=TriggerType.FOLLOW_UP, intensity="soft",
                           data={"topic": topic,
                                 "source": entry.get("source", ""),
                                 "age_hours": round(age, 1)}),
        "weight": w,
        "topic_ref": entry,
    }


def _should_morning(state: ChiguoState, now: datetime) -> bool:
    if state.cooldown.is_morning_sent():
        return False
    s = state.config.get("schedule", {})
    start, end = s.get("morning_start", 8), s.get("morning_end", 10)
    if not (start <= now.hour < end):
        return False
    # Poisson 决定是否触发（窗口内每分钟概率；经 [trigger].morning_probability 可配，默认 0.10）
    prob = _clamp01(state.config.get("trigger", {}).get("morning_probability", 0.10), 0.10)
    return random.random() < prob


def _should_night(state: ChiguoState, now: datetime) -> bool:
    if state.cooldown.is_night_sent():
        return False
    s = state.config.get("schedule", {})
    start, end = s.get("night_start", 20), s.get("night_end", 21)
    if not (start <= now.hour < end):
        return False
    # [trigger].night_probability 可配，默认 0.12
    prob = _clamp01(state.config.get("trigger", {}).get("night_probability", 0.12), 0.12)
    return random.random() < prob


def _should_meal(now: datetime, state: ChiguoState) -> bool:
    """饭点触发。上课时不发。"""
    if now.hour not in (11, 12, 17, 18, 19):
        return False
    # 上课时跳过（课间可以）
    if not _is_free_time(state, now):
        return False
    # [trigger].meal_probability 可配，默认 0.05
    prob = _clamp01(state.config.get("trigger", {}).get("meal_probability", 0.05), 0.05)
    return random.random() < prob


def _is_free_time(state: ChiguoState, now: datetime) -> bool:
    """主人是否空闲（非上课、非深夜）。"""
    # 深夜不开(配置默认 0-8;生物钟学习达标后为学习窗口)
    qs, qe = state.cooldown.quiet_window()
    if in_quiet_window(now, qs, qe):
        return False
    # 节假日/周末 → 空闲
    # #83: HolidayParser 构造失败降级（state.holiday_parser=None，chiguo_state 显式承诺
    # 使用点 None 防护）→ 无假日判定视为空闲，防 AttributeError 崩溃
    hp = state.holiday_parser
    if hp is None:
        return True
    if hp.is_holiday(now):
        return True
    if not hp.is_school_day(now):
        return True
    # 上课中 → 非空闲（schedule_status 门面:课表不可用 → None → 空闲）
    try:
        sch = state.schedule_status(now)
        if sch and sch.get("in_class"):
            return False
    except (ValueError, TypeError, OSError):
        pass
    return True


def _memory_should_trigger(mem: dict, now: datetime, trg_cfg: dict | None = None) -> bool:
    trg_cfg = trg_cfg or {}
    mtype = mem.get("type", "")
    if mtype == "reminder":
        # #79 去重：daemon 发送后在该 mem 上标记 last_triggered_at → 同进程不再重复触发
        if mem.get("last_triggered_at"):
            return False
        trigger_at = mem.get("trigger_at")
        if trigger_at:
            try:
                t = datetime.fromisoformat(trigger_at)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=CST)
                # #79: 不允许提前触发（now < trigger_at 直接排除）。
                # F-A5-01（#314 R9）: 窗口由「触发后 10min」放宽到 30min（1800s）。
                # 动机：发送侧 crontab 每 15 分钟 tick 一次（scripts/chiguo-tick.sh
                # / chiguo_daemon --loop 同一节拍）——原 10min 窗口 < 15min 节拍，
                # 存在整个窗口落在两次 tick 之间的空窗（审计：窗口可能被整个跳过）。
                # 窗口 ≥ 2×cron 间隔（30min ≥ 2×15min）→ 任意 15min tick 节拍下
                # 窗口内至少命中一次。仍由 last_triggered_at 在首次命中触发后去重，
                # 不引入每 tick 重复触发。
                delta = (now - t).total_seconds()
                return 0 <= delta < 1800
            except (ValueError, TypeError):
                return False
    elif mtype == "habit":
        window = mem.get("trigger_window", [])
        if not isinstance(window, list):
            return False  # B3: 非 list（int/str 等脏数据）视为未命中，防 in 判断 TypeError/错判
        # Q10: habit 触发概率经 [trigger].habit_probability 可配（默认 0.06）
        prob = _clamp01(trg_cfg.get("habit_probability", 0.06), 0.06)
        return now.hour in window and random.random() < prob
    return False
