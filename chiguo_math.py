# ============================================================
# chiguo_math.py — 数学工具：sigmoid、半衰期、Poisson
# ============================================================

import math
import random
from datetime import datetime as _dt


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

def weighted_trigger_choice(candidates: list[dict]) -> dict | None:
    """
    candidates: [{"type": str, "weight": float}, ...]
    按 weight 加权随机选一个。weights 不需要归一化。
    """
    if not candidates:
        return None
    total = sum(c["weight"] for c in candidates)
    if total <= 0:
        return random.choice(candidates)
    return random.choices(candidates, weights=[c["weight"] for c in candidates], k=1)[0]


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
    for ev in events:
        ev_time = ev.get("time")
        if isinstance(ev_time, str):
            ev_time = _dt.fromisoformat(ev_time)
        if ev_time is None:
            continue
        dt_hours = (now - ev_time).total_seconds() / 3600
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

    - 正常：λ_new = λ_old + growth_factor × held_count（累积增长）
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
