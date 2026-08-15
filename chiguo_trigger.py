# ============================================================
# chiguo_trigger.py — 触发器引擎 v2
# 用 sigmoid 概率加权随机选择，替代硬阈值排序
# ============================================================

import random
import math
import sys
from datetime import datetime
from dataclasses import dataclass, field

from chiguo_state import CST, ChiguoState
from chiguo_math import cfg_float, weighted_trigger_choice, in_quiet_window, mood_fresh


@dataclass
class Trigger:
    type: str
    intensity: str = "soft"
    data: dict = field(default_factory=dict)


# 情绪类触发集合 —— A3 日程乘数只作用于此集合；A4 activation = 该集合候选权重之和；
# A5 未回复退场（backing_off）时该集合禁发。仪式类（special/morning/night/meal/memory/follow_up）豁免。
EMOTION_TRIGGERS = frozenset({
    "lonely_low", "lonely_mid", "lonely_high",
    "anxiety", "playful", "reflect", "longing", "comfort",
})


def _clamp01(value, default: float) -> float:
    """#79: 配置阈值解析——非数值回退默认，数值钳制到 [0,1]。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


def _clamp_int(value, default: int, max_value: int | None = None) -> int:
    """#83: 退场阈值解析——非整数值（"3.5"/None 等）回退默认，负数钳制为 0（仿 _clamp01 惯例）。
    可选 max_value 钳制上限（如 backoff 阈值过大 = 静默禁用退场，属配置事故）。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    v = max(0, v)
    if max_value is not None:
        v = min(max_value, v)
    return v


def backoff_level(state: ChiguoState, now: datetime) -> int:
    """
    A5 未回复退场状态机（三态，不新增持久化字段——用现有 messages_without_reply 推导）：
      0 = normal      （< backoff_start 条未回复：正常竞争）
      1 = backing_off （backoff_start ≤ n < backoff_silent：情绪类禁发、仪式类照发）
      2 = silent      （n ≥ backoff_silent：全禁发；escape_valve longing 破防豁免——防死锁语义）
    参数：[cooldown].backoff_start=3 / backoff_silent=5。
    与 current_lambda() 的 0.7^n 退避（λ 降频）是两层独立机制：λ 降频 + 硬性禁发层，不冲突。
    now 保留作签名（未来可用 last_user_message_at 加时间窗扩展），当前只用计数推导。
    """
    cfg = state.config.get("cooldown", {})
    # #83: 类型防护——配置为 "3.5"/None 等非整数时回退默认，防 ValueError/TypeError 崩溃
    start = _clamp_int(cfg.get("backoff_start", 3), 3, max_value=100)
    silent = _clamp_int(cfg.get("backoff_silent", 5), 5, max_value=100)
    n = state.cooldown.messages_without_reply
    if n >= silent:
        return 2
    if n >= start:
        return 1
    return 0
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

    # ── A5: 未回复退场状态机（硬性禁发层，escape_valve 已在上面 return → 天然豁免）──
    # 0=normal 正常竞争；1=backing_off 情绪类禁发、仪式类照发；2=silent 全禁发。
    # 与 current_lambda 的 0.7^n 退避（λ 降频）是两层独立机制，不冲突。
    backoff = backoff_level(state, now)
    if backoff >= 2:
        return None

    weighted_candidates: list[dict] = []

    # 仪式触发权重缩放（默认为1.0，调低可减少仪式触发对情绪触发的压制）
    ritual_scale = cfg_float(state.config.get("cooldown", {}).get("ritual_weight_scale", 1.0), 1.0)

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
        if not isinstance(mem, dict):
            continue  # 数据防御：非 dict 条目跳过（state 加载已净化，这里再兜底防崩）
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
    elif (not state.pending_topics and state.memory_bridge.available
          and random.random() < 0.5):
        # 记忆兜底:近 48h 内、用户相关的记忆,选中一条作为接话茬素材(不落盘)
        # #83: ① 概率门控(50%)——pending_topics 为空时不得每 tick 无条件多关键词搜索
        # (热点 IO;与同文件 mem0 随机浮现块 silent_h>6 and random<0.08 同款降频风格);
        # ② timestamp 类型防护——ISO 字符串等非数值直接跳过,防 str > float TypeError。
        now_ts = now.timestamp()
        for mem in state.memory_bridge.user_relevant(limit=10, min_importance=0.4):
            ts = mem.get("timestamp") or 0
            if not ts or not isinstance(ts, (int, float)):
                continue
            ts = ts / 1000.0 if ts > 1e12 else ts  # epoch ms → s
            age = (now_ts - ts) / 3600
            if 0 < age <= fup_max:
                # C3: l0_abstract 已废弃，text 兜底为准。
                text = (mem.get("text") or mem.get("l0_abstract") or "").strip()[:50]
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
    # v10 (#73 A6): lonely_high 专属 0.3^n 阻尼已删除 → 统一 repeat 阻尼（候选收集后统一乘）覆盖
    raw_high = state.trigger_weight("lonely_high") * (1 - 0.4 * tsun) * rate_factor
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
    anx_baseline = _clamp01(trg_cfg.get("anxiety_baseline", 0.5), 0.5)
    anx_min_weight = _clamp01(trg_cfg.get("anxiety_min_weight", 0.3), 0.3)
    raw_anx = state.trigger_weight("anxiety")

    # ── v1.11 ①: 用户情绪感知（user_mood） ──
    # 新鲜窗口内（默认 6h）低落/崩溃 → comfort 安慰触发 + anxiety 权重加成。
    # 全部参数默认 0/关闭 → 行为恒等（灰度先例）。
    mood = state.cooldown.user_mood
    mood_fresh_flag = bool(mood and mood_fresh(
        mood, now, trg_cfg.get("user_mood_ttl_minutes", 360.0)))
    if mood_fresh_flag:
        # 低落时 anxiety 触发小幅加成（user_mood_anxiety_bonus 默认 0；仅 low/distressed）
        anx_bonus = trg_cfg.get("user_mood_anxiety_bonus", 0.0)
        try:
            anx_bonus = max(0.0, float(anx_bonus))
        except (TypeError, ValueError):
            anx_bonus = 0.0
        if anx_bonus > 0 and mood.get("mood") in ("low", "distressed"):
            raw_anx = raw_anx * (1.0 + anx_bonus * mood.get("intensity", 0.0))

    # #169: bonus 放大可能让 raw_anx 超过 1.0，而 denom=raw+baseline*(1-raw) 假设 [0,1]
    # → 先钳到 1.0 再归一化（无 bonus 时 raw 已为 sigmoid ∈ [0,1]，此钳制为 no-op）。
    raw_anx = min(1.0, raw_anx)

    # B2 (#137): A4 must_send 标定矛盾修复 —— 原归一化 w=raw/(raw+baseline) 把 w_anx
    # 钳在 max≈0.664 < must_send_activation(0.75)，anxiety 单源永远到不了高段必发。
    # 改为 w=raw/(raw+baseline*(1-raw))：raw→1 时 w→1（高焦虑可达 must_send 高段），
    # 中低段基本保持（anx=40 → ≈0.187 vs 原 0.171，仍 < anxiety_min_weight 0.3 不成候选）。
    denom_anx = raw_anx + anx_baseline * (1 - raw_anx)
    w_anx = raw_anx / denom_anx if denom_anx > 0 else 0.0
    if w_anx > anx_min_weight:
        weighted_candidates.append({
            "trigger": Trigger(type="anxiety", intensity="medium"),
            "weight": w_anx,
        })

    # comfort 安慰触发（对标 ESConv：感知到低落 → 更主动安慰）
    comfort_base = trg_cfg.get("comfort_weight_base", 0.0)
    try:
        comfort_base = float(comfort_base)
    except (TypeError, ValueError):
        comfort_base = 0.0
    if mood_fresh_flag and comfort_base > 0 and mood.get("mood") in ("low", "distressed"):
        # 强度 × 好感调制（高好感更想哄）；softmax 归一化防恒候选（同 anxiety 模式）
        raw_cf = comfort_base * mood.get("intensity", 0.0) * (1 + (emo.affection - 50) / 100)
        cf_baseline = _clamp01(trg_cfg.get("comfort_baseline", 0.5), 0.5)
        cf_min = _clamp01(trg_cfg.get("comfort_min_weight", 0.03), 0.03)
        w_cf = raw_cf / (raw_cf + cf_baseline) if raw_cf + cf_baseline > 0 else 0.0
        if w_cf > cf_min:
            weighted_candidates.append({
                "trigger": Trigger("comfort", "soft"),
                "weight": w_cf,
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
    base_lambda = cfg_float(state.config.get("poisson", {}).get("base_lambda", 0.25), 0.25)
    if state.is_longing_overflow() and base_lambda > 0:
        w_longing = min(0.5, (acc_lam / base_lambda - 1) * 0.3)
        if w_longing > 0.03:
            weighted_candidates.append({
                "trigger": Trigger(type="longing", intensity="soft",
                                   data={"held_count": held, "accumulated_lambda": round(acc_lam, 3)}),
                "weight": w_longing,
            })

    # ── A5: backing_off（1 级）→ 情绪类候选整体跳过，仪式类照发 ──
    # 过滤在加权选择前统一执行（不散落各收集块），仪式类候选不受影响。
    if backoff == 1:
        weighted_candidates = [
            c for c in weighted_candidates
            if c["trigger"].type not in EMOTION_TRIGGERS
        ]

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

    # ── v10 (#73): 触发层三段优化 — A3 日程乘数 → A6 repeat 阻尼 → A4 三段激活 ──
    # trg_cfg 已在 follow_up 段定义，此处复用。

    # A3 日程乘数 + 抖动：只作用于情绪类候选，仪式类（morning/night/meal/special/memory/follow_up）豁免。
    # 上课中 ×0.3；空闲（节假日/周末/课间）× free_multiplier（默认 1.2）；半忙 ×0.6。
    # 再乘 uniform(0.8, 1.2) 随机抖动防机械感。逃生阀已在函数首 return → 天然豁免。
    free_mult = cfg_float(trg_cfg.get("free_multiplier", 1.2), 1.2)
    sched_mult = _schedule_multiplier(state, now, free_mult)
    for c in weighted_candidates:
        if c["trigger"].type in EMOTION_TRIGGERS:
            c["weight"] *= sched_mult

    # A6 统一 repeat 阻尼：trigger_history 按 type 计数 n，weight ×= repeat_decay ** min(n, cap)。
    # 对所有 trigger 类型统一生效（daemon 发送时 append history，本层只读不写）。
    repeat_decay = _clamp01(trg_cfg.get("repeat_decay", 0.6), 0.6)
    # B1: repeat_cap 走 _clamp_int 兜底（字符串"3"/None 等脏配置回退默认 3，负数钳 0），
    # 与 min(n, repeat_cap) 的整型语义一致（裸取遇字符串会 TypeError）
    repeat_cap = _clamp_int(trg_cfg.get("repeat_cap", 3), 3)
    history = state.cooldown.trigger_history
    for c in weighted_candidates:
        n = sum(1 for t in history if t == c["trigger"].type)
        c["weight"] *= repeat_decay ** min(n, repeat_cap)

    # A4 三段激活阈值：activation = 情绪维度族取 max（#79 后 0.75 按单源标定）。
    # 孤独三级（lonely_low/mid/high）是同一孤独维度的互斥表达 → 族内求和；其余情绪
    # （anxiety/playful/reflect/longing/comfort）各自单源取 max。两股中低情绪叠加
    # （如孤独35+焦虑57 空闲）不再凑到高段，孤独族和（孤独≥45）或单源焦虑强才必发。
    # 低段（< min_activation）→ 情绪类退出竞争（等效低能量沉默，仪式类照发）；
    # 中段 → 现状加权随机；高段（>= must_send_activation）→ 情绪类加权随机必选
    # （仪式类本轮退让），选中结果标记 must_send: true。
    # activation 在抖动前计算 → 三段归属是逐状态的确定性属性（同状态不会因随机
    # uniform(0.8,1.2) 抖动在发/不发间随机翻转）。抖动随后全局乘，防机械感。
    # #79: 阈值解析钳制到 [0,1]（配置越界/非数值不破坏三段语义），并校验 min < must
    min_activation = _clamp01(trg_cfg.get("min_activation", 0.08), 0.08)
    must_send_activation = _clamp01(trg_cfg.get("must_send_activation", 0.75), 0.75)
    if min_activation >= must_send_activation:
        print(f"[trigger] WARNING: min_activation({min_activation:.2f}) >= "
              f"must_send_activation({must_send_activation:.2f}), A4 高段必发失效"
              f"（需 min < must，请检查 chiguo_proactive.toml [trigger]）",
              file=sys.stderr)
    emo_cands = [c for c in weighted_candidates if c["trigger"].type in EMOTION_TRIGGERS]
    ritual_cands = [c for c in weighted_candidates if c["trigger"].type not in EMOTION_TRIGGERS]
    activation = _activation_score(emo_cands)
    must_send = False
    # 抖动一次采样全局乘（防机械感）：各乘数逐项乘积、换序等价，故最终权重分布不变；
    # 仅在加权选择前统一作用于情绪类候选（仪式类豁免），不扰动已定的 activation 与三段归属。
    jitter = random.uniform(0.8, 1.2)
    for c in weighted_candidates:
        if c["trigger"].type in EMOTION_TRIGGERS:
            c["weight"] *= jitter

    # ── A2: 分类型回复率反馈闭环（reply_feedback_enabled 默认 0 关闭恒等，可灰度）──
    # 对标 revive-companion 的反馈闭环：低回复率类型 weight ×(1-damp)（降频），
    # 高回复率类型 ×(1+boost)（微加成）。damp/boost 为 0 → 恒等。统计源 =
    # 状态持久化的 cooldown.reply_stats（daemon 发送时 sent+1、--user-msg 收到
    # 回复时 replied+1），样本数 < min_samples 不调整（防冷启动误伤）。
    # 放在抖动后、三段选择前 → 只影响类型间相对概率，不扰动 A4 三段归属阈值。
    if trg_cfg.get("reply_feedback_enabled", 0):
        stats = getattr(state.cooldown, "reply_stats", None) or {}
        rfb_damp = cfg_float(trg_cfg.get("reply_feedback_damp", 0.0), 0.0)
        rfb_boost = cfg_float(trg_cfg.get("reply_feedback_boost", 0.0), 0.0)
        rfb_low = cfg_float(trg_cfg.get("reply_feedback_low_rate", 0.3), 0.3)
        rfb_high = cfg_float(trg_cfg.get("reply_feedback_high_rate", 0.7), 0.7)
        rfb_min = _clamp_int(trg_cfg.get("reply_feedback_min_samples", 3), 3)
        for c in weighted_candidates:
            st = stats.get(c["trigger"].type) or {}
            try:
                sent, replied = int(st.get("sent", 0)), int(st.get("replied", 0))
            except (TypeError, ValueError):
                continue
            if sent < rfb_min or sent <= 0:
                continue  # 样本不足 → 保持默认权重
            rate = replied / sent
            if rate < rfb_low:
                c["weight"] *= max(0.0, 1.0 - rfb_damp)
            elif rate >= rfb_high:
                c["weight"] *= max(0.0, 1.0 + rfb_boost)

    if activation >= must_send_activation and emo_cands:
        chosen = weighted_trigger_choice(emo_cands)
        must_send = True
    elif activation < min_activation:
        chosen = weighted_trigger_choice(ritual_cands)
    else:
        chosen = weighted_trigger_choice(weighted_candidates)
    if chosen is None:
        return None

    trigger = chosen["trigger"]
    if must_send:
        trigger.data["must_send"] = True

    # ── v7: 接话茬触发后标记已尝试(防重复;记忆兜底条目不在 pending 中,no-op)──
    if trigger.type == "follow_up" and chosen.get("topic_ref") is not None:
        state.mark_pending_topic_attempted(chosen["topic_ref"].get("topic", ""))

    # ── v4.1: 安全阀 — 连续崩溃降级（降级只改类型/强度，继承 data 保留 must_send 标记）──
    safety = state.safety_level(now)
    if safety >= 1 and trigger.type == "lonely_high":
        trigger = Trigger(type="lonely_mid", intensity="soft", data=trigger.data)
    elif safety >= 2:
        if trigger.type == "anxiety":
            trigger = Trigger(type="lonely_low", intensity="soft", data=trigger.data)
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
    except Exception:
        pass
    if _is_free_time(state, now):
        return free_mult
    return 0.6


def _activation_score(emo_cands: list[dict]) -> float:
    """A4 activation：按情绪维度族取 max（#79 后 must_send_activation=0.75 按单源标定）。

    孤独三级（lonely_low/mid/high）是同一孤独维度的互斥表达 → 族内求和（孤独总量压力）；
    其余情绪（anxiety/playful/reflect/longing/comfort）各自独立维度 → 单源取 max。
    与全量求和的差异：两股中低情绪叠加（如孤独35+焦虑57）不再凑到高段触发 must_send，
    只有单个维度真正强（孤独族和或单源焦虑 ≥ 阈值）才必发 —— 与 toml #79 文档承诺一致。"""
    lonely = sum(c["weight"] for c in emo_cands
                 if c["trigger"].type in ("lonely_low", "lonely_mid", "lonely_high"))
    others = [c["weight"] for c in emo_cands
              if c["trigger"].type not in ("lonely_low", "lonely_mid", "lonely_high")]
    return max(lonely, max(others, default=0.0))


def _followup_candidate(entry: dict, age: float, trg_cfg: dict) -> dict | None:
    """follow_up 候选组装（主块与记忆兜底块共用）。权重 = weight × 年龄钟形。
    #79: entry 缺 topic 键/非字符串/空白 → 跳过（防 KeyError 与空话题）。"""
    topic = entry.get("topic")
    if not isinstance(topic, str) or not topic.strip():
        return None
    # 峰值/钟形宽度配置防御：非数值回退默认；sigma<=0 → 回退 3.0（bell 分母
    # e^{-((age-peak)/sigma)^2}，sigma=0 会 ZeroDivisionError，兄弟配置键同有 clamp 防御）。
    try:
        peak = float(trg_cfg.get("follow_up_peak_hours", 4.0))
        sigma = float(trg_cfg.get("follow_up_sigma_hours", 3.0))
    except (TypeError, ValueError):
        peak, sigma = 4.0, 3.0
    if not math.isfinite(peak) or not math.isfinite(sigma) or sigma <= 0:
        peak, sigma = 4.0, 3.0
    bell = math.exp(-((age - peak) / sigma) ** 2)
    w = cfg_float(trg_cfg.get("follow_up_weight", 0.35), 0.35) * bell
    if w <= _clamp01(trg_cfg.get("follow_up_min_weight", 0.03), 0.03):
        return None
    return {
        "trigger": Trigger(type="follow_up", intensity="soft",
                           data={"topic": topic,
                                 "source": entry.get("source", ""),
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
    except Exception:
        pass
    return True


def _memory_should_trigger(mem: dict, now: datetime) -> bool:
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
                # #79: 窗口收紧为触发时刻之后 10 分钟内（不允许提前触发）
                delta = (now - t).total_seconds()
                return 0 <= delta < 600
            except (ValueError, TypeError):
                return False
    elif mtype == "habit":
        window = mem.get("trigger_window", [])
        if not isinstance(window, list):
            return False  # B3: 非 list（int/str 等脏数据）视为未命中，防 in 判断 TypeError/错判
        return now.hour in window and random.random() < 0.06
    return False
