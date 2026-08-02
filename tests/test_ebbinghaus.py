#!/usr/bin/env python3
"""test_ebbinghaus.py — Ebbinghaus 遗忘曲线测试（不依赖 LanceDB）"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import math
from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from memory_bridge import (
    MemoryBridge,
    DEFAULT_EBBINGHAUS_STRENGTH,
    DEFAULT_EBBINGHAUS_MIN_WEIGHT,
)


def test_ebbinghaus_new_memory():
    """新记忆权重接近 1"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    mem = {
        "timestamp": now.timestamp(),
        "importance": 0.8,
    }
    weight = bridge.ebbinghaus_weight(mem, now)
    assert weight > 0.95  # 非常新 → 非常高权重
    print(f"  OK test_ebbinghaus_new_memory: weight={weight:.3f}")


def test_ebbinghaus_old_memory():
    """旧记忆权重降低"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    # 100 天前的记忆
    old_ts = (now - timedelta(days=100)).timestamp()
    mem = {
        "timestamp": old_ts,
        "importance": 0.5,
    }
    weight = bridge.ebbinghaus_weight(mem, now)
    assert weight < 0.5  # 很旧 → 很低权重
    print(f"  OK test_ebbinghaus_old_memory: weight={weight:.4f}")


def test_ebbinghaus_important_decays_slower():
    """重要记忆衰减更慢"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    # 3 天前的记忆（足够不同但未触底）
    old_ts = (now - timedelta(days=3)).timestamp()

    mem_important = {"timestamp": old_ts, "importance": 0.9}
    mem_unimportant = {"timestamp": old_ts, "importance": 0.2}

    w_imp = bridge.ebbinghaus_weight(mem_important, now)
    w_unimp = bridge.ebbinghaus_weight(mem_unimportant, now)

    assert w_imp > w_unimp
    print(f"  OK test_ebbinghaus_important_decays_slower: imp={w_imp:.4f} unimp={w_unimp:.4f}")


def test_ebbinghaus_min_weight():
    """最低权重不归零"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    # 非常古老的记忆
    old_ts = (now - timedelta(days=1000)).timestamp()
    mem = {
        "timestamp": old_ts,
        "importance": 0.1,
    }
    weight = bridge.ebbinghaus_weight(mem, now)
    assert weight >= DEFAULT_EBBINGHAUS_MIN_WEIGHT
    print(f"  OK test_ebbinghaus_min_weight: weight={weight:.4f} >= {DEFAULT_EBBINGHAUS_MIN_WEIGHT}")


def test_ebbinghaus_custom_strength():
    """自定义强度参数生效"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    # 7 天前的记忆
    old_ts = (now - timedelta(days=7)).timestamp()
    mem = {"timestamp": old_ts, "importance": 0.5}

    # S=1000 (非常慢的遗忘) → 高权重
    w_slow = bridge.ebbinghaus_weight(mem, now, strength=1000.0)
    # S=24 (非常快的遗忘) → 低权重
    w_fast = bridge.ebbinghaus_weight(mem, now, strength=24.0)

    assert w_slow > w_fast
    print(f"  OK test_ebbinghaus_custom_strength: slow={w_slow:.4f} fast={w_fast:.4f}")


def test_ebbinghaus_zero_timestamp():
    """零时间戳 → 返回 1.0"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    mem = {"timestamp": 0, "importance": 0.5}
    weight = bridge.ebbinghaus_weight(mem, now)
    assert weight == 1.0
    print("  OK test_ebbinghaus_zero_timestamp")


def test_ebbinghaus_negative_age():
    """未来时间戳 → 权重 = 1.0（不惩罚）"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    future_ts = (now + timedelta(days=1)).timestamp()
    mem = {"timestamp": future_ts, "importance": 0.5}
    weight = bridge.ebbinghaus_weight(mem, now)
    assert weight == 1.0
    print("  OK test_ebbinghaus_negative_age")


def test_ebbinghaus_formula_math():
    """Ebbinghaus 公式 R = e^(-t/(S*imp)) 数学正确"""
    bridge = MemoryBridge.__new__(MemoryBridge)
    now = datetime.now(CST)
    t_hours = 168  # 恰好 7 天 = S
    mem = {
        "timestamp": (now - timedelta(hours=t_hours)).timestamp(),
        "importance": 1.0,
    }
    weight = bridge.ebbinghaus_weight(mem, now, strength=168.0)
    # R = e^(-168/(168*1)) = e^(-1) ≈ 0.368
    expected = math.exp(-1.0)
    assert abs(weight - expected) < 0.01
    print(f"  OK test_ebbinghaus_formula_math: weight={weight:.4f} ≈ {expected:.4f}")


if __name__ == "__main__":
    print("test_ebbinghaus.py\n")
    tests = [
        test_ebbinghaus_new_memory,
        test_ebbinghaus_old_memory,
        test_ebbinghaus_important_decays_slower,
        test_ebbinghaus_min_weight,
        test_ebbinghaus_custom_strength,
        test_ebbinghaus_zero_timestamp,
        test_ebbinghaus_negative_age,
        test_ebbinghaus_formula_math,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} ebbinghaus tests passed.")
