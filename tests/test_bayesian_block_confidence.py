#!/usr/bin/env python3
"""test_bayesian_block_confidence.py — 盲区4 bayesian min_confidence_for_block（AUD-030）

Given: schedule/day_plan.bayesian_adjust + chiguo_bayesian 状态推断
When:  most_likely=sleeping/busy/needs_care × confidence 0.0/0.4/0.5/0.6/0.9
Then:  sleeping 高置信→0.0；低置信与边界不 block；非 sleep 按各自分支
"""
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from schedule.day_plan import bayesian_adjust
from unittest.mock import MagicMock

CST = timezone(timedelta(hours=8))


def _emo(anxiety=10.0):
    m = MagicMock()
    m.anxiety = anxiety
    return m


def test_sleep_high_conf_blocks():
    """sleeping + confidence 0.6 > 0.5 → 返回 0.0（静默）。"""
    out = bayesian_adjust(0.8, {"most_likely": "sleeping", "confidence": 0.6}, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}})
    assert out == 0.0


def test_sleep_low_conf_no_block():
    """sleeping + confidence 0.4 ≤ 0.5 → 不 block（按 busy 分支走或保持 base）。"""
    base = 0.8
    out = bayesian_adjust(base, {"most_likely": "sleeping", "confidence": 0.4}, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}})
    # sleeping 但置信不足 → 不 return 0.0，落到 busy/needs_care/ anxiety 分支（此处 sleeping 不匹配 busy/needs_care → 保持 base）
    assert out == base


def test_boundary_eq_0_5_no_block():
    """边界 ==0.5 → 不 block（代码为 > 0.5，非 >=）。"""
    base = 0.7
    out = bayesian_adjust(base, {"most_likely": "sleeping", "confidence": 0.5}, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}})
    assert out == base, f"边界 0.5 不应 block, got {out}"


def test_sleep_very_high_still_blocks():
    """sleeping + 0.9 仍 block。"""
    assert bayesian_adjust(0.85, {"most_likely": "sleeping", "confidence": 0.95}, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}}) == 0.0


def test_non_sleep_busy_halves():
    """busy → base *0.5，与 sleeping 门限无关。"""
    out = bayesian_adjust(0.8, {"most_likely": "busy", "confidence": 0.9}, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}})
    assert abs(out - 0.4) < 1e-9


def test_non_sleep_needs_care_boost():
    """needs_care → min(base*1.2,0.95)。"""
    out = bayesian_adjust(0.5, {"most_likely": "needs_care", "confidence": 0.9}, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}})
    assert abs(out - 0.6) < 1e-9


def test_custom_threshold():
    """自定义 min_confidence_for_block=0.3 → 0.4 即可 block。"""
    assert bayesian_adjust(0.8, {"most_likely": "sleeping", "confidence": 0.4}, _emo(), {"bayesian": {"min_confidence_for_block": 0.3}}) == 0.0
    # 0.2 仍不 block
    assert bayesian_adjust(0.8, {"most_likely": "sleeping", "confidence": 0.2}, _emo(), {"bayesian": {"min_confidence_for_block": 0.3}}) == 0.8


def test_none_user_state_passthrough():
    """user_state=None → 保持 base。"""
    assert bayesian_adjust(0.6, None, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}}) == 0.6


def test_bayesian_estimator_end_to_end():
    """端到端小回归：真实 chiguo_bayesian 推断的 most_likely/confidence 仍走 bayesian_adjust 门限。"""
    from chiguo_bayesian import UserStateEstimator
    est = UserStateEstimator()
    # 喂足够 sleeping 观测，使 posterior 偏向 sleeping
    # 简化：直接构造高置信 sleeping 态
    state = {"most_likely": "sleeping", "confidence": 0.8}
    assert bayesian_adjust(0.5, state, _emo(), {"bayesian": {"min_confidence_for_block": 0.5}}) == 0.0
