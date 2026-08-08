# ============================================================
# chiguo_trigger.py — 触发器引擎 v2
# 用 sigmoid 概率加权随机选择，替代硬阈值排序
# ============================================================

import random
import math
from datetime import datetime
from dataclasses import dataclass, field

from chiguo_state import CST, ChiguoState
from chiguo_math import weighted_trigger_choice, in_quiet_window


@dataclass
class Trigger:
    type: str
    intensity: str = "soft"
    data: dict = field(default_factory=dict)


def evaluate_triggers(state: ChiguoState, now: datetime,
                      trigger_scale: dict | None = None) -> Trigger | None:
    """
    评估触发。
    v2 改进：不再硬排序取优先级最高者。
    而是：先收集所有合法候选 → 按 sigmoid 权重随机选一个。
    结果：53 孤独 + 48 不安时，lonely_mid 和 anxiety 都有概率被选中。
    v9 schedule-center:trigger_scale = 计划文件修饰参数,
    候选收集后统一乘,只改类型间相对概率;逃生阀在缩放前 return,天然豁免。
    """
    # ── v6: 溢出逃生阀 — 死锁态破防（高焦虑阻塞+沉默超限），强制最高优先 ──
    # 高焦虑阻塞（≥ anxiety_block_threshold）时，正常的 longing overflow
    # 数学上永远无法到达（accumulation 被 blocked），必须用时间+状态驱动破防。
    # longing_break_eligible 检查：① 焦虑 ≥ 阻塞阈值 ② 墙钟沉默 ≥ 72h ③ 冷却期外
    if state.longing_break_eligible(now):
        return Trigger("longing", "high", data={"escape_valve": True})

    weighted_candidates: list[dict] = []

    # 仪式触发权重缩放（默认为1.0，调低可减少仪式触发对情绪触发的压制）
    ritual_scale = state.config.get("cooldown", {}).get("ritual_weight_scale", 1.0)

    # ── 固定事件（权重固定，会概率性参与竞争） ──────────

    # 特殊日期(schedule-center 3c:数据源 = anniversary_mgr 当天匹配,原读 toml special_dates)
    try:
        anniv_today = state.anniversary_mgr.get_today(now.date())
        special_hit = any(a.type == "anniversary" for a in anniv_today)
    except Exception:
        special_hit = False
    if special_hit:
        weighted_candidates.append({
            "trigger": Trigger(type="special", intensity="soft"),
            "weight": 3.0 * ritual_scale,  # 高权重,但非绝对
        })

    # 早安
    if _should_morning(state, now):
        weighted_candidates.append({
            "trigger": Trigger(type="morning", intensity="soft"),
            "weight": 2.5 * ritual_scale,
        })

    # 晚安
    if _should_night(state, now):
        weighted_candidates.append({
            "trigger": Trigger(type="night", intensity="soft"),
            "weight": 2.0 * ritual_scale,
        })

    # 用餐（上课时跳过）
    if _should_meal(now, state):
        weighted_candidates.append({
            "trigger": Trigger(type="meal", intensity="soft"),
            "weight": 0.8 * ritual_scale,
        })

    # ── 记忆触发 ──
    # 两层：① JSON 手动记忆（习惯提醒等）② mem0 随机回忆
    for mem in state.memories:
        if _memory_should_trigger(mem, now):
            weighted_candidates.append({
                "trigger": Trigger(type="memory", intensity="soft",
                                   data={"memory": mem}),
                "weight": 2.0 * ritual_scale,
            })

    # mem0 随机浮现（低概率，仅在主人沉默时）
    # 如果 mem0 不可用，自动跳过
    silent_h = state.cooldown.silent_hours(now)
    if silent_h > 6 and random.random() < 0.08 and state.memory_bridge.available:
        mem0_mem = state.memory_bridge.random_memory(min_importance=0.4)
        if mem0_mem:
            weighted_candidates.append({
                "trigger": Trigger(type="memory", intensity="soft",
                                   data={"mem0_memory": mem0_mem}),
                "weight": 1.5 * ritual_scale,
            })

    # ── v7: 接话茬(follow_up)触发 ──
    # 待接续话题(analysis topic,2-48h 内)优先;无待接续话题 → 近期用户相关记忆兜底。
    # 权重 = follow_up_weight × 年龄钟形(峰值 follow_up_peak_hours)。
    # 触发后标记 attempted(单次尝试);过期话题顺带清理。
    trg_cfg = state.config.get("trigger", {})
    fup_min = trg_cfg.get("follow_up_min_age_hours", 2.0)
    fup_max = trg_cfg.get("follow_up_max_age_hours", 48.0)
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
                weighted_candidates.append(cand)
    elif not state.pending_topics and state.memory_bridge.available:
        # 记忆兜底:近 48h 内、用户相关的记忆,选中一条作为接话茬素材(不落盘)
        now_ts = now.timestamp()
        for mem in state.memory_bridge.user_relevant(limit=10, min_importance=0.4):
            ts = mem.get("timestamp") or 0
            if not ts:
                continue
            ts = ts / 1000.0 if ts > 1e12 else ts  # epoch ms → s
            age = (now_ts - ts) / 3600
            if 0 < age <= fup_max:
                text = (mem.get("l0_abstract") or mem.get("text") or "").strip()[:50]
                if text:
                    follow_entries.append(
                        ({"topic": text, "source": "memory",
                          "created_at": now.isoformat()}, age))
                    break
        if follow_entries:
            cand = _followup_candidate(follow_entries[0][0], follow_entries[0][1], trg_cfg)
            if cand:
                weighted_candidates.append(cand)

    # ── 情绪驱动事件（sigmoid 权重） ─────────────────────
    emo = state.emotion

    # 孤独三级：权重 = sigmoid(孤独值) × 傲娇修正 × 变化率因子
    # 傲娇高 → 嘴硬中位触发更易（不愿示弱）；傲娇低 → 崩溃触发更易（防线已软）
    tsun = emo.tsundere_index / 100  # 0.1~0.95
    # 变化率因子：暴涨（>1.5/h）→ urgency 高，权重放大（孤独+不安）
    lo_rate = emo.loneliness_rate
    anx_rate = emo.anxiety_rate
    rate_factor = 1.0 + max(0, (lo_rate - 1.5) * 0.3) + max(0, (anx_rate - 2.0) * 0.2)

    raw_low = state.trigger_weight("lonely_low") * (1 + 0.3 * tsun) * rate_factor
    raw_mid = state.trigger_weight("lonely_mid") * (1 + 0.5 * tsun) * rate_factor
    # 连续崩溃降级：24h 内再次 lonely_high → 权重衰减
    recent_high = sum(1 for t in state.cooldown.trigger_history[-3:] if t == "lonely_high")
    high_decay = 0.3 ** recent_high
    raw_high = state.trigger_weight("lonely_high") * (1 - 0.4 * tsun) * rate_factor * high_decay
    # Softmax 式归一化：三个触发互斥，改一个中点 → 另两个自动重分配
    total = raw_low + raw_mid + raw_high + 0.5  # 0.5 = "不触发"基线
    w_low = raw_low / total
    w_mid = raw_mid / total
    w_high = raw_high / total

    if w_low > 0.03:
        weighted_candidates.append({
            "trigger": Trigger(type="lonely_low", intensity="soft"),
            "weight": w_low,
        })

    if w_mid > 0.03:
        weighted_candidates.append({
            "trigger": Trigger(type="lonely_mid", intensity="medium"),
            "weight": w_mid,
        })

    if w_high > 0.02:
        weighted_candidates.append({
            "trigger": Trigger(type="lonely_high", intensity="intense"),
            "weight": w_high,
        })

    # anxiety：与孤独三级同款 "不触发基线" softmax 归一化（raw / (raw + baseline)）
    # v7 修复：原 0.05 硬门槛在 anxiety=40（raw≈0.103）即恒候选，沉默期确定性发满日上限。
    # 归一化后 40 → ≈0.171 < anxiety_min_weight(0.3)，不再成为唯一候选；高焦虑仍强候选。
    anx_baseline = trg_cfg.get("anxiety_baseline", 0.5)
    anx_min_weight = trg_cfg.get("anxiety_min_weight", 0.3)
    raw_anx = state.trigger_weight("anxiety")
    w_anx = raw_anx / (raw_anx + anx_baseline) if raw_anx + anx_baseline > 0 else 0.0
    if w_anx > anx_min_weight:
        weighted_candidates.append({
            "trigger": Trigger(type="anxiety", intensity="medium"),
            "weight": w_anx,
        })

    # 好感度调制：高好感 → 甜蜜触发权重上升
    aff_factor = 1 + (emo.affection - 50) / 100  # 0.5~1.5

    # boredom / playful 触发：高元气 + 沉默适中 + 非上课
    if emo.energy > 70 and 2 < silent_h < 48 and _is_free_time(state, now):
        # v4: 外向性调制 playful
        pers_extra = getattr(getattr(state, 'personality', None), 'extraversion', 60.0)
        pers_extra_factor = 0.5 + (pers_extra / 100) * 1.0  # 0.5~1.5
        w_bored = 0.15 * (emo.energy / 100) * aff_factor * pers_extra_factor
        if w_bored > 0.03:
            weighted_candidates.append({
                "trigger": Trigger(type="playful", intensity="soft"),
                "weight": w_bored,
            })

    # ── v4: reflect 触发（角色内省）──
    # 条件：高好感 + 低沉默 + 高元气 + 低神经质
    pers = getattr(state, 'personality', None)
    if pers:
        neuroticism = getattr(pers, 'neuroticism', 60.0)
        if (emo.affection > 70 and silent_h < 2 and emo.energy > 60
                and neuroticism < 70 and random.random() < 0.08):
            w_reflect = 0.08 * (emo.affection / 100) * (1 - neuroticism / 100) * (emo.energy / 100)
            if w_reflect > 0.02:
                weighted_candidates.append({
                    "trigger": Trigger(type="reflect", intensity="soft"),
                    "weight": w_reflect,
                })

    # ── v4: longing 触发（概率累积溢出）──
    # held_count 高 + accumulated_lambda 高 → "累积的想念终于溢出"
    held = getattr(state.cooldown, 'held_count', 0)
    acc_lam = state.cooldown.accumulated_lambda or 0
    base_lambda = state.config.get("poisson", {}).get("base_lambda", 0.25)
    if state.is_longing_overflow() and base_lambda > 0:
        w_longing = min(0.5, (acc_lam / base_lambda - 1) * 0.3)
        if w_longing > 0.03:
            weighted_candidates.append({
                "trigger": Trigger(type="longing", intensity="soft",
                                   data={"held_count": held, "accumulated_lambda": round(acc_lam, 3)}),
                "weight": w_longing,
            })

    if not weighted_candidates:
        return None

    # ── schedule-center:计划文件修饰参数(§5.2,拷问 18)──
    # 单点缩放:统一乘 scale.get(type, scale.get("default", 1.0));不动 13 处候选逻辑。
    # 逃生阀 longing 已在函数首 return → 天然豁免。共同缩放因子会被概率竞争约掉,
    # 只改变类型间相对概率(写 default 全局缩放无实际效果)。
    if trigger_scale:
        for c in weighted_candidates:
            c["weight"] *= trigger_scale.get(c["trigger"].type,
                                             trigger_scale.get("default", 1.0))

    # ── 加权随机选择（而非硬排序取max） ──────────────────
    chosen = weighted_trigger_choice(weighted_candidates)
    if chosen is None:
        return None

    trigger = chosen["trigger"]

    # ── v7: 接话茬触发后标记已尝试(防重复;记忆兜底条目不在 pending 中,no-op)──
    if trigger.type == "follow_up" and chosen.get("topic_ref") is not None:
        state.mark_pending_topic_attempted(chosen["topic_ref"].get("topic", ""))

    # ── v4.1: 安全阀 — 连续崩溃降级 ──
    safety = state.safety_level(now)
    if safety >= 1 and trigger.type == "lonely_high":
        trigger = Trigger(type="lonely_mid", intensity="soft")
    elif safety >= 2:
        if trigger.type == "anxiety":
            trigger = Trigger(type="lonely_low", intensity="soft")
        else:
            trigger.intensity = "soft"

    return trigger


def _followup_candidate(entry: dict, age: float, trg_cfg: dict) -> dict | None:
    """follow_up 候选组装（主块与记忆兜底块共用）。权重 = weight × 年龄钟形。"""
    peak = trg_cfg.get("follow_up_peak_hours", 4.0)
    sigma = trg_cfg.get("follow_up_sigma_hours", 3.0)
    bell = math.exp(-((age - peak) / sigma) ** 2)
    w = trg_cfg.get("follow_up_weight", 0.35) * bell
    if w <= trg_cfg.get("follow_up_min_weight", 0.03):
        return None
    return {
        "trigger": Trigger(type="follow_up", intensity="soft",
                           data={"topic": entry["topic"],
                                 "source": entry["source"],
                                 "age_hours": round(age, 1)}),
        "weight": w,
        "topic_ref": entry,
    }


def _should_morning(state: ChiguoState, now: datetime) -> bool:
    if state.cooldown.morning_sent:
        return False
    s = state.config.get("schedule", {})
    start, end = s.get("morning_start", 8), s.get("morning_end", 10)
    if not (start <= now.hour < end):
        return False
    # Poisson 决定是否触发（窗口内每分钟概率）
    return random.random() < 0.10


def _should_night(state: ChiguoState, now: datetime) -> bool:
    if state.cooldown.night_sent:
        return False
    s = state.config.get("schedule", {})
    start, end = s.get("night_start", 20), s.get("night_end", 21)
    if not (start <= now.hour < end):
        return False
    return random.random() < 0.12


def _should_meal(now: datetime, state: ChiguoState) -> bool:
    """饭点触发。上课时不发。"""
    if now.hour not in (11, 12, 17, 18, 19):
        return False
    # 上课时跳过（课间可以）
    if not _is_free_time(state, now):
        return False
    return random.random() < 0.05


def _is_free_time(state: ChiguoState, now: datetime) -> bool:
    """主人是否空闲（非上课、非深夜）。"""
    # 深夜不开(配置默认 0-8;生物钟学习达标后为学习窗口)
    qs, qe = state.cooldown.quiet_window()
    if in_quiet_window(now, qs, qe):
        return False
    # 节假日/周末 → 空闲
    if state.holiday_parser.is_holiday(now):
        return True
    if not state.holiday_parser.is_school_day(now):
        return True
    # 上课中 → 非空闲（schedule_status 门面:课表不可用 → None → 空闲）
    try:
        sch = state.schedule_status(now)
        if sch and sch.get("in_class"):
            return False
    except Exception:
        pass
    return True


def _memory_should_trigger(mem: dict, now: datetime) -> bool:
    mtype = mem.get("type", "")
    if mtype == "reminder":
        trigger_at = mem.get("trigger_at")
        if trigger_at:
            try:
                t = datetime.fromisoformat(trigger_at)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=CST)
                return abs((now - t).total_seconds()) < 600
            except (ValueError, TypeError):
                return False
    elif mtype == "habit":
        window = mem.get("trigger_window", [])
        return now.hour in window and random.random() < 0.06
    return False
