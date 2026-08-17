# ============================================================
# chiguo_math.py — 数学工具：sigmoid、半衰期、Poisson
# ============================================================

import math
import random
from datetime import datetime as _dt

from chiguo_time import CST  # Q22: 共享时区常量



# ── 配置浮点解析（Q25 收敛）──────────────────────────────
# 统一 composer chiguo_trigger._to_float 与 netease.service._cfg_float 三份重复实现。

def cfg_float(value, default: float, clamp_min: float | None = None) -> float:
    """配置浮点解析——非数值/NaN/inf 回退默认；clamp_min 非空时对数值结果做下限钳制。

    - float("nan")/float("inf") 不抛异常，会毒化权重（weight *= nan）；math.isfinite 兜底。
    - clamp_min 用于 netease 的 retry_backoff_seconds/reprobe_minutes 等正数域字段
      （负值钳制为 0）；composer/trigger 不传 clamp_min，负值语义由调用处 max(0.0,·) 兜底。
    """
    try:
        fv = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(fv):
        return default
    if clamp_min is not None and fv < clamp_min:
        fv = clamp_min
    return fv


# ── Sigmoid（逻辑函数）────────────────────────────────────
# 替代硬阈值：x 在 midpoint 附近柔和过渡，k 控制陡峭度

def sigmoid(x: float, midpoint: float = 50, steepness: float = 0.1) -> float:
    """返回 0~1 的概率值。x=midpoint 时返回 0.5。"""
    return 1.0 / (1.0 + math.exp(-steepness * (x - midpoint)))


# ── 半衰期衰减 ────────────────────────────────────────────
# 替代线性乘法：情绪值回到一半所需的时间 = half_life 小时

def decay(current: float, elapsed_hours: float, half_life_hours: float) -> float:
    """经过 elapsed_hours 后，值还剩多少。half_life 是半衰期（小时）。"""
    if elapsed_hours <= 0:
        return current
    if half_life_hours <= 0:
        return current  # ponytail: guard against zero/negative half-life
    return current * (2.0 ** (-elapsed_hours / half_life_hours))


# ── 半衰期恢复（向目标值靠拢） ────────────────────────────

def recover(current: float, target: float, elapsed_hours: float, half_life_hours: float) -> float:
    """向 target 靠拢，半衰期为 half_life 小时。"""
    if elapsed_hours <= 0:
        return current
    if half_life_hours <= 0:
        return current  # ponytail: guard against zero/negative half-life
    gap = target - current
    decay_factor = 2.0 ** (-elapsed_hours / half_life_hours)
    return current + gap * (1.0 - decay_factor)


# ── 弹性衰减恢复（A1：偏离越远回弹越快） ────────────────────

def elastic_recover(
    current: float,
    target: float,
    elapsed_hours: float,
    half_life_hours: float,
    baseline: float = 100.0,
) -> float:
    """
    弹性恢复：有效半衰期随偏离程度缩短。
    effective_hl = half_life / (1 + |target - current| / baseline)
    偏离 target 越远回弹越快；接近 target 时 ≈ 原半衰期。
    baseline 默认 100（情绪值域）。baseline <= 0 → 退化为普通 recover（防除零）。
    """
    if baseline <= 0:
        return recover(current, target, elapsed_hours, half_life_hours)
    effective_hl = half_life_hours / (1.0 + abs(target - current) / baseline)
    return recover(current, target, elapsed_hours, effective_hl)


# ── A2: 情绪交互矩阵 ────────────────────────────────────
# tick() 情绪推进后调用一次。3 条规则幅度全部参数化到 cfg
# （[emotion].interaction_*，乘数=1.0 默认关闭 → 行为恒等，可安全灰度）。

def apply_interaction_matrix(emotion: dict, cfg: dict) -> dict:
    """
    返回新 emotion dict（不就地修改）。
    规则（k 为 cfg 中对应乘数，默认 1.0 = 关闭恒等；>1.0 增强幅度）：
    1. affection > 60 → anxiety 恢复加速：anxiety *= 1 - 0.02*k*affection/100
    2. energy < 30 → loneliness 恢复加速：loneliness *= 1 + 0.02*k*(30-energy)/30
    3. anxiety > 70 → energy 恢复减速：energy *= 1 - 0.01*k
    """
    out = dict(emotion)

    def _k(key: str, default: float = 1.0) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return default

    k1 = _k("interaction_affection_anxiety")
    if k1 != 1.0 and out.get("affection", 0.0) > 60:
        out["anxiety"] = out["anxiety"] * (1.0 - 0.02 * k1 * out["affection"] / 100.0)

    k2 = _k("interaction_energy_loneliness")
    if k2 != 1.0 and out.get("energy", 0.0) < 30:
        out["loneliness"] = out["loneliness"] * (1.0 + 0.02 * k2 * (30.0 - out["energy"]) / 30.0)

    k3 = _k("interaction_anxiety_energy")
    if k3 != 1.0 and out.get("anxiety", 0.0) > 70:
        out["energy"] = out["energy"] * (1.0 - 0.01 * k3)

    return out


# ── ④ 情绪基线长期漂移（关系动力学） ──────────────────────
# 对标 astrbot_plugin_emotion_state_machine 的 GROUP/RELATION_BASELINE 概念：
# 长期互动缓慢移动情绪收敛目标（基线），与人格层 regress_to_baseline（防人格
# 漂移）分层——"人格稳定、情绪基线可漂移"。

def baseline_shift_of(interaction: dict) -> dict:
    """
    事件 → 基线漂移方向表 {dim: ±1}。纯函数，可精确断言。
    - user_reply + latency very_slow/slow → 冷落：loneliness↑、affection↓
    - user_reply + warmth<−0.2 → 冷淡：loneliness/anxiety↑、affection↓
    - user_reply + warmth>0.3 → 温柔：anxiety↓、affection↑
    - character_send + was_replied=False → 未回复：loneliness/anxiety↑、affection↓
    其余（快回/中性/被回复）→ 零漂移。
    """
    shift = {"loneliness": 0, "anxiety": 0, "affection": 0}
    itype = interaction.get("type", "")
    try:
        warmth = float(interaction.get("warmth", 0.0))
    except (TypeError, ValueError):
        warmth = 0.0
    lat_cat = interaction.get("latency_category", "normal")

    if itype == "user_reply":
        if lat_cat in ("slow", "very_slow"):
            shift["loneliness"] += 1
            shift["affection"] -= 1
        if warmth < -0.2:
            shift["loneliness"] += 1
            shift["anxiety"] += 1
            shift["affection"] -= 1
        elif warmth > 0.3:
            shift["anxiety"] -= 1
            shift["affection"] += 1
    elif itype == "character_send":
        if not interaction.get("was_replied", False):
            shift["loneliness"] += 1
            shift["anxiety"] += 1
            shift["affection"] -= 1
    return shift


# ── ② 情绪自然波动（OU 过程 + 动态上限） ──────────────────
# 对标 lacuna_core FluctuationEngine：小幅带均值回归的噪声模拟情绪自然起伏。
# OU 连续化公式对不规则 Δt（60s~24h+）数学一致，优于 1/f 粉红噪声（滤波器状态
# 难在 daemon 每次新建进程时重建）；独立 random.Random 实例防全局序列污染。

def ou_step(value: float, target: float, theta: float, sigma: float,
            dt_hours: float, rng: random.Random) -> float:
    """一步 OU：x += θ(μ−x)Δt + σ·√Δt·ε。dt<=0 或 sigma<=0 → 恒等。"""
    if dt_hours <= 0 or sigma <= 0:
        return value
    pull = theta * (target - value) * dt_hours
    shock = sigma * math.sqrt(dt_hours) * rng.gauss(0.0, 1.0)
    return value + pull + shock


def noise_cap(step_magnitude: float, raw_noise: float) -> float:
    """动态上限：噪声绝对值 ≤ 0.5 × 本次弹性步进量（防 噪声>信号 反噬）。
    step_magnitude<=0（gap≈0 的稳态）→ 噪声压到 0。"""
    cap = 0.5 * abs(step_magnitude)
    return max(-cap, min(cap, raw_noise))


# ── ① 用户情绪感知（user_mood） ────────────────────────────
# 对标 thu-coai/Emotional-Support-Conversation（ACL 2021）：共情先感知
# 求助者情绪类型+强度。LLM 只报 mood/intensity，幅度由系数表决定（决策零 LLM）。

# 基础方向表（× intensity × cfg 系数；系数默认 0 = 关闭灰度）
MOOD_DELTA = {
    "low":        {"anxiety": +2.0, "affection": +0.5},   # 心疼 → 不安略升、想靠近
    "distressed": {"anxiety": +3.0, "affection": +1.0},   # 更强烈
    "happy":      {"energy": +2.0,  "affection": +1.0},   # 被感染 → 元气回升
    "angry":      {"anxiety": +2.0, "affection": -1.0},   # 不安升、好感微降（克制）
}

MOOD_NOTE = {
    "low":        "（哥哥似乎心情低落（强度 {i:.1f}），语气比平时更温柔克制，少一点嘴硬，主动关心一句就好，不过度怜悯、不质问）",
    "distressed": "（哥哥情绪很低落（强度 {i:.1f}），放下嘴硬，认真陪他说话，语气温柔坚定，给安全感，不卖惨不质问）",
    "happy":      "（哥哥今天心情很好（强度 {i:.1f}），气氛轻松，可以更活泼一点接梗）",
    "angry":      "（哥哥在生气（强度 {i:.1f}），语气放软、不顶嘴、给台阶下，先顺毛再说话）",
}


def user_mood_impact(mood: str, intensity: float, cfg: dict) -> dict:
    """
    user_mood → 情绪 delta。纯函数，可精确断言。
    delta = MOOD_DELTA[mood][dim] × intensity × cfg["user_mood_<mood>_<dim>_factor"]
    - calm / intensity<=0 / 系数 0 → {}（零效果）
    - 未知 mood（调用方已归一化，防御性返回 {}）
    """
    if mood not in MOOD_DELTA or intensity <= 0:
        return {}
    out = {}
    for dim, base in MOOD_DELTA[mood].items():
        try:
            k = float(cfg.get(f"user_mood_{mood}_{dim}_factor", 0.0))
        except (TypeError, ValueError):
            k = 0.0
        if k != 0.0:
            out[dim] = base * intensity * k
    return out


def user_mood_note(kind: str, intensity: float) -> str:
    """user_mood → 语气注解（注入 _build_context guidance）。calm/未知 → 空串。"""
    tpl = MOOD_NOTE.get(kind)
    if not tpl or intensity <= 0:
        return ""
    return tpl.format(i=intensity)


# ── 自身情绪注解表 self_mood_note（Issue #356）──────────────────
# 迟菓自身情绪 → 中文语气注解（最主导 1-2 条，注入 _build_context guidance）。
# 与 energy_note 互补：energy 档注解属既有 energy_note，本表专注
# loneliness/affection/anxiety/tsundere 组合语义；主导优先级：
# 委屈难过(kernel 级) > 开心 > 高好感 > 高傲娇 > 中孤独；非命中 → ""。
# 措辞参照人格文件『情绪武器』：开心得意=感叹号+行动证明；委屈难过=省略号碎句；
# 嘴硬=短句连发+否认+反问；并始终与角色铁律协同（被夸不得直接开心 → 开心注解
# 保留"先嘴硬、行动回应"约束）。缺键容错：缺失维度按非命中处理。纯函数，
# 不依赖 chiguo_state dataclass（调用方传 dict，参照 apply_interaction_matrix）。

def self_mood_note(emotion: dict) -> str:
    """
    自身情绪 → 语气注解（最多 1-2 条，按主导情绪优先级）。
    - 委屈难过: loneliness > 70 且 anxiety > 60   （kernel 级，最优先）
    - 开心:     energy > 80 且 loneliness < 30 且 affection > 70
    - 高好感:   affection > 70
    - 高傲娇:   tsundere_index > 80
    - 中孤独:   loneliness > 50
    非命中区间 → 空串；缺键维度按非命中处理（防御性，不抛异常）。
    任意主导下同时命中傲娇底色（tsundere_index > 80）→ 叠加 1 条嘴硬注脚
    （共 ≤2 条）——嘴硬是人格底色而非仅主导态，开心也受铁律⑦约束。
    """
    def _num(key: str) -> float | None:
        v = emotion.get(key)
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    loneliness = _num("loneliness")
    affection = _num("affection")
    anxiety = _num("anxiety")
    energy = _num("energy")
    tsundere = _num("tsundere_index")

    notes: list[str] = []
    # ① 委屈难过（kernel 级，最优先）
    if loneliness is not None and anxiety is not None and loneliness > 70 and anxiety > 60:
        notes.append(
            f"（此刻有点委屈难过（孤独{loneliness:.0f}，不安{anxiety:.0f}），"
            "碎句加省略号，可以流露一点真实的脆弱，不质问不卖惨）"
        )
    # ② 开心（铁律⑦协同：被夸仍先嘴硬，行动回应不直接承认）
    elif (energy is not None and loneliness is not None and affection is not None
          and energy > 80 and loneliness < 30 and affection > 70):
        notes.append(
            "（当前心情偏开心，语气可活泼些，感叹号加行动证明，"
            "但被夸仍先嘴硬，行动回应不直接承认）"
        )
    # ③ 高好感
    elif affection is not None and affection > 70:
        notes.append("（对哥哥好感偏高，想亲近但口是心非，关心带上刺）")
    # ④ 高傲娇
    elif tsundere is not None and tsundere > 80:
        notes.append("（此刻傲娇上头，短句连发，先否认再反问，语气嘴硬心软）")
    # ⑤ 中孤独（以上均不命中时兜底）
    elif loneliness is not None and loneliness > 50:
        notes.append("（有点想哥哥了，试探着问两句，但绝不直说等待）")
    # 傲娇底色叠加注脚（任意主导下共 ≤2 条）
    if notes and tsundere is not None and tsundere > 80 and "傲娇上头" not in notes[0]:
        notes.append("（嘴硬底色仍在，先推开再接受）")
    return "；".join(notes)


def mood_fresh(mood: dict | None, now, ttl_minutes: float = 360.0) -> bool:
    """
    user_mood 感知是否仍在有效窗口内（TTL 默认 6h）。
    mood 为 None / 缺 at / 坏时间戳 → False（不感知）。
    """
    if not isinstance(mood, dict) or not mood.get("at"):
        return False
    try:
        at = _dt.fromisoformat(mood["at"])
    except (ValueError, TypeError):
        return False
    if at.tzinfo is None:
        at = at.replace(tzinfo=CST)
    try:
        age_minutes = (now - at).total_seconds() / 60.0
    except TypeError:
        return False
    return 0 <= age_minutes <= ttl_minutes


# ── ③ 回复影响惯性阻尼 ──────────────────────────────────
# 单条 analysis delta 幅度压缩（默认 inertia=0 → 恒等，可灰度）。
# 对标 lacuna_core InertiaFilter：负向权重更高（inertia_neg 独立键）。

def impact_inertia(
    delta: float,
    inertia: float,
    inertia_neg: float,
    affection_mod: float = 0.0,
    affection: float = 50.0,
) -> float:
    """
    单条回复影响阻尼：effective_delta = delta × (1 - inertia_eff)。

    - delta < 0（负向，如冷淡回复的不安回升）→ 用 inertia_neg（可设更高，
      参考 lacuna_core 负向权重 1.5 vs 1.0 先例）
    - affection_mod > 0 → 亲密度调制：好感偏离 50 越远，阻尼缩放越大
      （好感高 → 阻尼小，更易被哄好/更快被伤）
    - inertia_eff 钳制 [0, 0.9]（永不反向、永不归零）
    - inertia 与 inertia_neg 均 ≤ 0 → 恒等返回（默认关闭灰度）
    """
    sign = -1.0 if delta < 0 else 1.0
    base = inertia_neg if sign < 0 else inertia
    if base <= 0:
        return delta
    eff = base * (1.0 - affection_mod * (affection - 50.0) / 100.0)
    eff = max(0.0, min(eff, 0.9))
    return delta * (1.0 - eff)


# ── A10: 回复饱和阻尼 ────────────────────────────────────
# 30 分钟窗口内同向回复事件越多，情绪加成越小（防刷）。

def drop_damp(recents: int, factor: float = 0.5, cap: int = 3) -> float:
    """
    饱和阻尼系数：同向加成 × drop_damp。
    recents = 窗口内已发生的同向回复事件数（不含本次）。
    第 1 次 → 1.0；第 2 次 → factor；…；第 cap 次起 → factor^cap。
    负 factor 配置钳制为 0（防交替符号导致情绪反向）。
    """
    factor = max(float(factor), 0.0)
    return factor ** min(max(recents, 0), cap)


# ── 动态 λ 计算 ──────────────────────────────────────────

def dynamic_lambda(
    loneliness: float,
    anxiety: float,
    base_lambda: float = 0.3,
    loneliness_mid: float = 50,
    loneliness_k: float = 0.1,
    anxiety_mid: float = 45,
    anxiety_k: float = 0.08,
) -> float:
    """
    计算动态事件率 λ。
    λ = base × sigmoid(孤独) × sigmoid(不安)
    孤独和不安越高，λ 越大，触发越频繁。
    """
    lo_factor = sigmoid(loneliness, loneliness_mid, loneliness_k)
    anx_factor = sigmoid(anxiety, anxiety_mid, anxiety_k)
    return base_lambda * lo_factor * anx_factor


# ── 加权随机选择 ─────────────────────────────────────────
# 多个触发候选时，按 sigmoid 概率加权选择而非硬排序

def weighted_trigger_choice(candidates: list[dict], rng=random) -> dict | None:
    """
    candidates: [{"type": str, "weight": float}, ...]
    按 weight 加权随机选一个。weights 不需要归一化。
    rng: 随机源(默认全局 random 模块),可注入 random.Random 实例防全局序列污染。
    F-A5-06 (#315 R13)：total<=0（全 0/全负权重，含 trigger_scale 全 0 禁发意图）
    → 返回 None 不选；修复前 rng.choice 均匀随机兜底 → "0 权重=禁用"意图静默失效。
    调用方（chiguo_trigger/chiguo_topics）均已处理 None（无候选 → 跳过该分支）。
    """
    if not candidates:
        return None
    total = sum(c["weight"] for c in candidates)
    if total <= 0:
        return None
    return rng.choices(candidates, weights=[c["weight"] for c in candidates], k=1)[0]


# ── Hawkes 自激过程 ─────────────────────────────────────
# 替代 Poisson 独立假设：每次触发让 λ 短暂升高，随后指数衰减
# λ(t) = μ + Σ α × exp(-β × (t - t_i))

def hawkes_intensity(
    base_mu: float,
    events: list[dict],
    now,
    alpha: float = 0.3,
    beta: float = 0.5,
    window_hours: float = 24.0,
) -> float:
    """
    Hawkes 过程密度，带指数衰减核。

    λ(t) = base_mu + Σ α × exp(-β × (t - t_i))

    - events: [{"type": str, "time": datetime | str}, ...]
    - alpha: 单词事件的激发强度
    - beta: 衰减速率 (1/小时)，半衰期 = ln(2)/β。β=0.5 → ~1.39h
    - window_hours: 超过此时间的事件忽略（完全衰减）

    now 参数接受 datetime 或可调 .timestamp() 的对象。
    """
    intensity = base_mu
    if isinstance(now, str):
        now = _dt.fromisoformat(now)
    if isinstance(now, _dt) and now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    for ev in events:
        ev_time = ev.get("time")
        if isinstance(ev_time, str):
            try:
                ev_time = _dt.fromisoformat(ev_time)
            except ValueError:
                continue  # 单条坏时间戳跳过，不影响整链
        if ev_time is None:
            continue
        # naive 事件时间（旧状态迁移/手改文件可能写入无 tz 时间戳）→ 补 CST，防 TypeError
        if ev_time.tzinfo is None:
            ev_time = ev_time.replace(tzinfo=CST)
        try:
            dt_hours = (now - ev_time).total_seconds() / 3600
        except TypeError:
            continue  # 单条坏时间戳跳过，不影响整链
        if 0 < dt_hours < window_hours:  # ponytail: skip events at now (dt=0) — they are the current event, not historical excitation
            intensity += alpha * math.exp(-beta * dt_hours)
    return intensity


# ── 概率累积（v4） ────────────────────────────────────────
# 参考 revive-companion 的 Poisson 概率累积机制
# 不发消息时 longing 概率递增；但高焦虑时阻塞（"生气了不会找你"）

def longing_accumulate(
    current_lambda: float,
    base_lambda: float,
    growth_factor: float = 0.08,
    anxiety: float = 0.0,
    anxiety_block_threshold: float = 70.0,
    held_count: int = 0,
    max_lambda_multiplier: float = 5.0,
) -> tuple[float, bool]:
    """
    概率累积机制。

    - 正常：λ_new = λ_old + growth_factor × max(held_count, 1)（累积增长，至少按 1 份计）
    - 焦虑超高（anxiety > anxiety_block_threshold）：不累积
      （"生气了不会主动找你"，需用户回复重置）
    - λ 上限 = base_lambda × max_lambda_multiplier

    Returns:
        (new_lambda, was_blocked)
        was_blocked: True if accumulation was blocked by high anxiety
    """
    # 焦虑阻塞
    if anxiety >= anxiety_block_threshold:
        return current_lambda, True

    # 累积增长
    new_lambda = current_lambda + growth_factor * max(held_count, 1)

    # 上限
    cap = base_lambda * max_lambda_multiplier
    new_lambda = min(new_lambda, cap)

    return new_lambda, False


def longing_decay(
    current_lambda: float,
    base_lambda: float,
    decay_factor: float = 0.5,
) -> float:
    """
    用户回复后 longing 回退。
    λ 向 base_lambda 回归，不回到底（保留部分惯性）。
    """
    return base_lambda + (current_lambda - base_lambda) * decay_factor


def in_quiet_window(dt: _dt, start: int, end: int) -> bool:
    """跨午夜静默窗口判定:end < start → [start, 24)∪[0, end);否则 [start, end)。"""
    if end < start:
        return dt.hour >= start or dt.hour < end
    return start <= dt.hour < end


# ── 内容级查重（A9） ─────────────────────────────────────
# topic 候选与最近已发消息文本做 3-gram Jaccard 查重，防复读。

def jaccard_3gram(a: str, b: str) -> float:
    """
    文本相似度：按字符（中文按字）滑窗 3-gram 集合的 Jaccard 系数。
    Jaccard = |A∩B| / |A∪B|，值域 [0, 1]。

    - 长度 < 3 的文本退化为整串字符集合（短-短比较仍可比对；与 ≥3 字文本比较
      因字符集与 3-gram 集无交集恒为 0，属预期）。
    - 任一文本为空 → 返回 0.0（空集与任何集合无重叠）。
    """
    def _grams(s: str) -> set:
        s = (s or "").strip()
        if not s:
            return set()
        if len(s) < 3:
            return set(s)
        return {s[i:i + 3] for i in range(len(s) - 2)}

    ga, gb = _grams(a), _grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)
