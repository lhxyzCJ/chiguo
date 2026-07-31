#!/usr/bin/env python3
"""test_longing.py — 概率累积机制单元测试"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chiguo_math import longing_accumulate, longing_decay


def test_normal_accumulation():
    """正常累积：λ 递增"""
    base = 0.25
    lam, blocked = longing_accumulate(0.25, base, growth_factor=0.08, anxiety=30.0,
                                       held_count=1)
    assert lam > 0.25
    assert not blocked
    print(f"  OK test_normal_accumulation: 0.25 → {lam:.4f}")


def test_anxiety_block():
    """高焦虑阻塞累积"""
    base = 0.25
    lam, blocked = longing_accumulate(0.25, base, growth_factor=0.08, anxiety=75.0,
                                       anxiety_block_threshold=70.0, held_count=1)
    assert lam == 0.25  # 不变
    assert blocked
    print(f"  OK test_anxiety_block: blocked={blocked}, lam={lam:.4f}")


def test_max_cap():
    """λ 不超过上限"""
    base = 0.25
    lam, blocked = longing_accumulate(1.5, base, growth_factor=0.5, anxiety=30.0,
                                       held_count=10, max_lambda_multiplier=5.0)
    assert lam <= base * 5.0  # max = 1.25
    print(f"  OK test_max_cap: lam={lam:.4f} (cap={base*5.0:.4f})")


def test_held_count_effect():
    """held_count 越大累积越多"""
    base = 0.25
    lam1, _ = longing_accumulate(0.25, base, growth_factor=0.08, anxiety=30.0, held_count=1)
    lam5, _ = longing_accumulate(0.25, base, growth_factor=0.08, anxiety=30.0, held_count=5)
    assert lam5 > lam1
    print(f"  OK test_held_count_effect: held=1→{lam1:.4f}, held=5→{lam5:.4f}")


def test_longing_decay():
    """用户回复后 λ 回退"""
    base = 0.25
    accumulated = 1.0  # 累积到很高
    new_lam = longing_decay(accumulated, base, decay_factor=0.5)
    # 应该回退：0.25 + (1.0 - 0.25) * 0.5 = 0.625
    assert 0.4 < new_lam < 0.9
    print(f"  OK test_longing_decay: {accumulated:.4f} → {new_lam:.4f}")


def test_anxiety_at_threshold():
    """焦虑恰好在阈值：阻塞（>= 阈值）"""
    lam, blocked = longing_accumulate(0.25, 0.25, growth_factor=0.08, anxiety=70.0,
                                       anxiety_block_threshold=70.0, held_count=1)
    assert blocked  # 70.0 >= 70.0 → 阻塞
    print(f"  OK test_anxiety_at_threshold: blocked={blocked}")


def test_anxiety_just_above_threshold():
    """焦虑略高于阈值：阻塞"""
    lam, blocked = longing_accumulate(0.25, 0.25, growth_factor=0.08, anxiety=70.1,
                                       anxiety_block_threshold=70.0, held_count=1)
    assert blocked
    print(f"  OK test_anxiety_just_above_threshold: blocked={blocked}")


def test_longing_decay_full_recovery():
    """decay_factor=0 → 完全回退到 base"""
    base = 0.25
    new_lam = longing_decay(1.5, base, decay_factor=0.0)
    assert new_lam == 0.25
    print(f"  OK test_longing_decay_full_recovery: {new_lam}")


if __name__ == "__main__":
    print("test_longing.py\n")
    tests = [
        test_normal_accumulation,
        test_anxiety_block,
        test_max_cap,
        test_held_count_effect,
        test_longing_decay,
        test_anxiety_at_threshold,
        test_anxiety_just_above_threshold,
        test_longing_decay_full_recovery,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} longing tests passed.")
