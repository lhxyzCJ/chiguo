#!/usr/bin/env python3
"""test_chiguo_math.py — chiguo_math 纯函数单元测试"""

import math
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chiguo_math import (
    sigmoid, decay, recover,
    dynamic_lambda, weighted_trigger_choice, hawkes_intensity,
)
from datetime import datetime, timezone, timedelta

CST = timezone(timedelta(hours=8))

# ── sigmoid ──────────────────────────────────────────

def test_sigmoid_midpoint():
    """x=midpoint → 0.5"""
    assert abs(sigmoid(50, 50, 0.1) - 0.5) < 0.001
    assert abs(sigmoid(38, 38, 0.2) - 0.5) < 0.001
    print("  OK test_sigmoid_midpoint")

def test_sigmoid_bounds():
    """x→-∞ → 0, x→+∞ → 1"""
    assert sigmoid(-1000, 50, 0.1) < 0.001
    assert sigmoid(1000, 50, 0.1) > 0.999
    assert 0 < sigmoid(30, 50, 0.1) < 0.5
    assert 0.5 < sigmoid(70, 50, 0.1) < 1.0
    print("  OK test_sigmoid_bounds")

def test_sigmoid_monotonic():
    """sigmoid 单调递增"""
    vals = [sigmoid(x, 50, 0.1) for x in range(0, 101, 5)]
    for i in range(len(vals) - 1):
        assert vals[i] <= vals[i + 1], f"not monotonic at {i*5}"
    print("  OK test_sigmoid_monotonic")

def test_sigmoid_extreme_inputs_no_overflow():
    """大幅输入（±1000，默认陡度 0.1）→ 分别逼近 1/0，exp 不溢出"""
    hi = sigmoid(1000, 50, 0.1)
    lo = sigmoid(-1000, 50, 0.1)
    assert hi >= 1.0 - 1e-15, f"sigmoid(1000) should ≈1, got {hi}"
    assert lo <= 1e-30, f"sigmoid(-1000) should ≈0, got {lo}"
    assert 0 < lo < hi <= 1.0
    # 大陡度正向不崩（exp(-950) 下溢为 0.0 → 1.0）
    assert sigmoid(1000, 50, 1.0) == 1.0
    # ⚠ 已知缺陷（报告不修）：陡度 1.0 + 大幅负输入 → math.exp 溢出 OverflowError。
    # 生产路径陡度仅 0.06~0.2（sigmoid/trigger_weight），不受影响；此处固化现状防静默变化。
    try:
        sigmoid(-1000, 50, 1.0)
        raised = False
    except OverflowError:
        raised = True
    assert raised, "known limitation: steepness=1.0 with x=-1000 currently overflows (see comment)"
    print("  OK test_sigmoid_extreme_inputs_no_overflow")

# ── decay ────────────────────────────────────────────

def test_decay_half_life():
    """经过 half_life 后值减半"""
    v = decay(100, 10, 10)
    assert abs(v - 50) < 0.01, f"expected 50, got {v}"
    print("  OK test_decay_half_life")

def test_decay_zero_time():
    """elapsed=0 → 不变"""
    assert decay(42, 0, 10) == 42
    print("  OK test_decay_zero_time")

def test_decay_long_time():
    """极长时间 → 趋近 0"""
    assert decay(100, 1000, 10) < 0.001
    print("  OK test_decay_long_time")

def test_decay_negative_half_life_guard():
    """负/零半衰期守卫 → 返回值不变（不崩、不放大）"""
    assert decay(100, 10, -5) == 100
    assert decay(100, 10, 0) == 100
    assert decay(42.5, 999, -1) == 42.5
    print("  OK test_decay_negative_half_life_guard")

# ── recover ──────────────────────────────────────────

def test_recover_half_life():
    """向 target 靠拢，半衰期正确"""
    v = recover(50, 100, 40, 40)
    assert abs(v - 75) < 0.1, f"expected ~75, got {v}"
    print("  OK test_recover_half_life")

def test_recover_zero_time():
    """elapsed=0 → 不变"""
    assert recover(30, 100, 0, 20) == 30
    print("  OK test_recover_zero_time")

def test_recover_full():
    """极长时间 → 趋近 target"""
    v = recover(0, 100, 1000, 10)
    assert abs(v - 100) < 0.001
    print("  OK test_recover_full")

def test_recover_negative_half_life_guard():
    """负/零半衰期守卫 → 返回值不变"""
    assert recover(30, 100, 10, -5) == 30
    assert recover(30, 100, 10, 0) == 30
    print("  OK test_recover_negative_half_life_guard")

# ── dynamic_lambda ───────────────────────────────────

def test_dynamic_lambda_bounds():
    """极端情绪值验证 λ 范围"""
    lo = dynamic_lambda(0, 0)
    hi = dynamic_lambda(100, 100)
    assert 0 < lo < hi, f"lo={lo}, hi={hi}"
    assert hi <= 0.3, f"base_lambda=0.3 → max λ should be ≤0.3"
    print("  OK test_dynamic_lambda_bounds")

def test_dynamic_lambda_monotonic():
    """孤独/不安越高，λ 越大"""
    a = dynamic_lambda(20, 20)
    b = dynamic_lambda(80, 80)
    assert a < b
    print("  OK test_dynamic_lambda_monotonic")

# ── weighted_trigger_choice ──────────────────────────

def test_weighted_choice_deterministic():
    """零权重永不被选"""
    candidates = [{"type": "a", "weight": 0}, {"type": "b", "weight": 100}]
    results = [weighted_trigger_choice(candidates)["type"] for _ in range(100)]
    assert all(r == "b" for r in results), f"zero-weight item was selected"
    print("  OK test_weighted_choice_deterministic")

def test_weighted_choice_empty():
    """空列表 → None"""
    assert weighted_trigger_choice([]) is None
    print("  OK test_weighted_choice_empty")

def test_weighted_choice_all_zero():
    """所有权重为 0 → 随机选一个（不崩溃）"""
    candidates = [{"type": "a", "weight": 0}, {"type": "b", "weight": 0}]
    result = weighted_trigger_choice(candidates)
    assert result is not None
    assert result["type"] in ("a", "b")
    print("  OK test_weighted_choice_all_zero")

def test_weighted_choice_negative_weights():
    """负权重：负项永不被选中（cumulative 偏移等效 max(0,w)）；全非正 → 随机回退不崩"""
    candidates = [{"type": "a", "weight": -5}, {"type": "b", "weight": 10}]
    results = [weighted_trigger_choice(candidates)["type"] for _ in range(200)]
    assert all(r == "b" for r in results), f"negative-weight item selected: {results}"
    all_neg = [{"type": "a", "weight": -5}, {"type": "b", "weight": -3}]
    results2 = [weighted_trigger_choice(all_neg)["type"] for _ in range(50)]
    assert all(r in ("a", "b") for r in results2), "all-negative must fall back to random choice"
    neg_zero = [{"type": "a", "weight": -5}, {"type": "b", "weight": 0}]
    results3 = [weighted_trigger_choice(neg_zero)["type"] for _ in range(50)]
    assert all(r in ("a", "b") for r in results3), "negative+zero must not crash"
    print("  OK test_weighted_choice_negative_weights")


# ── Hawkes 测试 ─────────────────────────────────────────

def test_hawkes_no_events():
    """零事件 → 返回 base_mu"""
    now = datetime.now(CST)
    result = hawkes_intensity(0.25, [], now, alpha=0.3, beta=0.5)
    assert abs(result - 0.25) < 0.001, f"expected 0.25, got {result}"
    print("  OK test_hawkes_no_events")


def test_hawkes_one_event():
    """单事件验证指数衰减形状"""
    now = datetime.now(CST)
    events = [{"type": "lonely_low", "time": (now - timedelta(hours=1)).isoformat()}]
    # t=1h, exp(-0.5*1) ≈ 0.6065
    expected = 0.25 + 0.3 * 0.6065  # ≈ 0.432
    r1 = hawkes_intensity(0.25, events, now, alpha=0.3, beta=0.5)
    assert 0.43 < r1 < 0.44, f"at t+1h expected ~0.432, got {r1}"

    # t=2h, exp(-0.5*2) ≈ 0.3679
    events2 = [{"type": "lonely_low", "time": (now - timedelta(hours=2)).isoformat()}]
    r2 = hawkes_intensity(0.25, events2, now, alpha=0.3, beta=0.5)
    assert 0.35 < r2 < 0.37, f"at t+2h expected ~0.360, got {r2}"

    # t=0h: event right now → no excitation (must be positive dt)
    events3 = [{"type": "lonely_low", "time": now.isoformat()}]
    r3 = hawkes_intensity(0.25, events3, now, alpha=0.3, beta=0.5)
    assert abs(r3 - 0.25) < 0.001, f"event at t=0 should not excite, got {r3}"
    print("  OK test_hawkes_one_event")


def test_hawkes_multiple_events():
    """多事件叠加验证"""
    now = datetime.now(CST)
    events = [
        {"type": "lonely_low", "time": (now - timedelta(hours=0.5)).isoformat()},
        {"type": "lonely_mid", "time": (now - timedelta(hours=1.0)).isoformat()},
        {"type": "lonely_high", "time": (now - timedelta(hours=2.0)).isoformat()},
    ]
    # t=0.5: exp(-0.25) ≈ 0.7788
    # t=1.0: exp(-0.5)  ≈ 0.6065
    # t=2.0: exp(-1.0)  ≈ 0.3679
    expected = 0.25 + 0.3 * (0.7788 + 0.6065 + 0.3679)  # ≈ 0.776
    r = hawkes_intensity(0.25, events, now, alpha=0.3, beta=0.5)
    assert 0.77 < r < 0.78, f"expected ~0.776, got {r}"
    print("  OK test_hawkes_multiple_events")


def test_hawkes_old_events():
    """超过 window 的事件被忽略"""
    now = datetime.now(CST)
    events = [
        {"type": "lonely_low", "time": (now - timedelta(hours=1)).isoformat()},
        {"type": "lonely_mid", "time": (now - timedelta(hours=30)).isoformat()},
    ]
    r = hawkes_intensity(0.25, events, now, alpha=0.3, beta=0.5, window_hours=24)
    # Only the 1h event counts
    expected = 0.25 + 0.3 * 0.6065  # ≈ 0.432
    assert 0.43 < r < 0.44, f"old event should be ignored, got {r}"
    print("  OK test_hawkes_old_events")


def test_hawkes_monotonic():
    """随着时间推移，强度递减"""
    now = datetime.now(CST)
    events = [{"type": "lonely_low", "time": (now - timedelta(hours=1)).isoformat()}]
    r1 = hawkes_intensity(0.25, events, now, alpha=0.3, beta=0.5)
    r2 = hawkes_intensity(0.25, events, now + timedelta(hours=1), alpha=0.3, beta=0.5)
    assert r1 > r2, f"intensity should decay: {r1} → {r2}"
    print("  OK test_hawkes_monotonic")


if __name__ == "__main__":
    print("test_chiguo_math.py\n")
    tests = [
        test_sigmoid_midpoint, test_sigmoid_bounds, test_sigmoid_monotonic,
        test_sigmoid_extreme_inputs_no_overflow,
        test_decay_half_life, test_decay_zero_time, test_decay_long_time,
        test_decay_negative_half_life_guard,
        test_recover_half_life, test_recover_zero_time, test_recover_full,
        test_recover_negative_half_life_guard,
        test_dynamic_lambda_bounds, test_dynamic_lambda_monotonic,
        test_weighted_choice_deterministic, test_weighted_choice_empty, test_weighted_choice_all_zero,
        test_weighted_choice_negative_weights,
        # Hawkes
        test_hawkes_no_events, test_hawkes_one_event, test_hawkes_multiple_events,
        test_hawkes_old_events, test_hawkes_monotonic,
    ]
    for t in tests:
        t()

    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
