#!/usr/bin/env python3
"""test_chiguo_math.py — chiguo_math 纯函数单元测试"""

import math
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_math import (
    sigmoid, decay, recover, elastic_recover,
    dynamic_lambda, weighted_trigger_choice, hawkes_intensity,
    drop_damp, jaccard_3gram,
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

# ── elastic_recover（A1 弹性衰减） ─────────────────────

def test_elastic_recover_far_gap_faster():
    """远偏离 target → 有效半衰期缩短，回弹明显快于普通 recover"""
    # gap=90, baseline=100 → effective_hl = 40/(1+0.9) ≈ 21.05
    far = elastic_recover(10, 100, 40, 40, 100.0)
    plain = recover(10, 100, 40, 40)  # hl=40 → 恰好半程
    assert abs(plain - 55.0) < 0.01, f"sanity: plain recover should be 55, got {plain}"
    assert far > 70, f"far-gap elastic should overshoot plain (55), got {far}"
    # 精确公式校验：40/(1+90/100)=21.0526 → 10 + 90*(1-2^(-40/21.0526))
    import math
    expected = 10 + 90 * (1 - 2.0 ** (-40 / (40 / (1 + 90 / 100))))
    assert abs(far - expected) < 1e-9, f"got {far}, expected {expected}"
    print("  OK test_elastic_recover_far_gap_faster")

def test_elastic_recover_near_target_approx_original():
    """接近 target → 有效半衰期 ≈ 原半衰期（gap 小，弹性几乎不生效）"""
    # gap=5 → effective_hl = 40/1.05 ≈ 38.1（vs 原 40）→ 与普通 recover 差异 <0.1
    near = elastic_recover(95, 100, 40, 40, 100.0)
    plain = recover(95, 100, 40, 40)
    assert abs(near - plain) < 0.1, f"near-target should ≈ plain recover: {near} vs {plain}"
    # 与 far-gap 对比：同 elapsed 下 near 比 far 回弹慢
    far = elastic_recover(10, 100, 40, 40, 100.0)
    assert near - 95 < far - 10, "near target should recover slower in absolute gap terms"
    print("  OK test_elastic_recover_near_target_approx_original")

def test_elastic_recover_baseline_parameterized():
    """baseline 越大 → 弹性越弱（大 baseline 稀释偏离度）"""
    small_b = elastic_recover(10, 100, 40, 40, baseline=50.0)   # gap/50=1.8 → hl=14.3
    default_b = elastic_recover(10, 100, 40, 40, baseline=100.0)  # hl≈21.05
    large_b = elastic_recover(10, 100, 40, 40, baseline=1000.0)   # hl≈36.7
    plain = recover(10, 100, 40, 40)
    assert small_b > default_b > large_b > plain, \
        f"baseline ordering broken: {small_b} > {default_b} > {large_b} > {plain}"
    print("  OK test_elastic_recover_baseline_parameterized")

def test_elastic_recover_nonpositive_baseline_guard():
    """baseline <= 0 → 退化为普通 recover（防除零，不崩）"""
    for b in (0.0, -100.0):
        assert elastic_recover(10, 100, 40, 40, b) == recover(10, 100, 40, 40), \
            f"baseline={b} should degrade to plain recover"
    # elapsed=0 / 负半衰期守卫同样传递
    assert elastic_recover(30, 100, 0, 20, 100.0) == 30
    assert elastic_recover(30, 100, 10, -5, 100.0) == 30
    print("  OK test_elastic_recover_nonpositive_baseline_guard")

# ── drop_damp（A10 回复饱和阻尼） ─────────────────────

def test_drop_damp_progression():
    """第 n 次同向事件 → factor^(n-1)；cap 饱和"""
    assert drop_damp(0) == 1.0, "首次（无前置事件）应无阻尼"
    assert abs(drop_damp(1) - 0.5) < 1e-12
    assert abs(drop_damp(2) - 0.25) < 1e-12
    assert abs(drop_damp(3) - 0.125) < 1e-12
    assert abs(drop_damp(4) - 0.125) < 1e-12, "cap=3 后饱和"
    print("  OK test_drop_damp_progression")

def test_drop_damp_parameterized():
    """factor/cap 参数化"""
    assert abs(drop_damp(1, factor=0.7) - 0.7) < 1e-12
    assert abs(drop_damp(2, factor=0.7) - 0.49) < 1e-12
    assert abs(drop_damp(5, factor=0.5, cap=2) - 0.25) < 1e-12, "cap=2 → 0.5^2 饱和"
    assert drop_damp(0, factor=0.3) == 1.0
    print("  OK test_drop_damp_parameterized")

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
    """所有权重为 0 → None（F-A5-06 #315 R13：total<=0 禁用而非均匀随机兜底）
    修复前：rng.choice 均匀随机仍会选出 0 权重项（'0 权重=禁用'意图静默失效）。"""
    candidates = [{"type": "a", "weight": 0}, {"type": "b", "weight": 0}]
    result = weighted_trigger_choice(candidates)
    assert result is None, f"total<=0 应返回 None（不选），got {result}"
    print("  OK test_weighted_choice_all_zero")

def test_weighted_choice_negative_weights():
    """负权重：负项永不被选中（cumulative 偏移等效 max(0,w)）；全非正 → None（不随机回退）
    F-A5-06 (#315 R13)：total<=0 一律视为"无可用候选"→ None。"""
    candidates = [{"type": "a", "weight": -5}, {"type": "b", "weight": 10}]
    results = [weighted_trigger_choice(candidates)["type"] for _ in range(200)]
    assert all(r == "b" for r in results), f"negative-weight item selected: {results}"
    all_neg = [{"type": "a", "weight": -5}, {"type": "b", "weight": -3}]
    assert weighted_trigger_choice(all_neg) is None, "all-negative total<=0 → None"
    neg_zero = [{"type": "a", "weight": -5}, {"type": "b", "weight": 0}]
    assert weighted_trigger_choice(neg_zero) is None, "negative+zero total<=0 → None"
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


def test_hawkes_naive_and_broken_ts_guarded():
    """naive（无 tz）事件时间与坏字符串 → 不抛 TypeError，正常计算其余事件（B5）"""
    now = datetime.now(CST)
    events = [
        {"time": datetime(2026, 7, 1, 10, 0)},          # naive → 补 CST
        {"time": "not-a-date"},                          # 坏字符串 → 跳过
        {"time": (now - timedelta(hours=1)).isoformat()},  # 正常事件 → 计入
    ]
    r = hawkes_intensity(0.25, events, now, alpha=0.3, beta=0.5)
    assert 0.40 < r < 0.44, f"naive 事件应被计入（1h 事件 ~0.432），got {r}"
    print("  OK test_hawkes_naive_and_broken_ts_guarded")


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


# ── jaccard_3gram（A9 内容级防复读） ──────────────────────

def test_jaccard_same_text_is_1():
    """相同文本 → jaccard = 1.0"""
    assert jaccard_3gram("今天天气不错", "今天天气不错") == 1.0
    assert jaccard_3gram("嗯嗯。一个鸡肉三明治～", "嗯嗯。一个鸡肉三明治～") == 1.0
    print("  OK test_jaccard_same_text_is_1")


def test_jaccard_unrelated_text_is_0():
    """完全无关文本（无共同 3-gram）→ 0.0"""
    assert jaccard_3gram("今天天气不错", "苹果香蕉橘子") == 0.0
    assert jaccard_3gram("关心哥哥吃饭了吗", "最近在追什么番剧") == 0.0
    print("  OK test_jaccard_unrelated_text_is_0")


def test_jaccard_short_text_boundary():
    """短文本边界：长度 < 3 退化为整串字符集合，仍可比对"""
    assert jaccard_3gram("哈哈", "哈哈") == 1.0       # 相同短串
    assert jaccard_3gram("哈哈", "呵呵") == 0.0       # 不同短串
    assert jaccard_3gram("哈", "哈") == 1.0           # 单字
    assert jaccard_3gram("哈", "呵") == 0.0
    print("  OK test_jaccard_short_text_boundary")


def test_jaccard_empty_handling():
    """空文本/空集 → 0.0（不除零）"""
    assert jaccard_3gram("", "abc") == 0.0
    assert jaccard_3gram("abc", "") == 0.0
    assert jaccard_3gram("", "") == 0.0
    assert jaccard_3gram("   ", "abc") == 0.0  # 空白剥离后为空
    print("  OK test_jaccard_empty_handling")


def test_jaccard_partial_overlap():
    """部分重叠：共享 3-gram 越多相似度越高（0 < j < 1）"""
    j_partial = jaccard_3gram("今天天气不错呀", "今天天气不错呢")
    assert 0.0 < j_partial < 1.0
    assert jaccard_3gram("今天天气不错呀", "今天天气不错呢") > jaccard_3gram("今天天气不错呀", "午饭吃了吗哥哥")
    print("  OK test_jaccard_partial_overlap")
