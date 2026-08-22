#!/usr/bin/env python3
"""test_bayesian_invariant.py — T09 TDD invariant (RED→GREEN).

验证:
1. TRANSITIONS 行和=1.0 (单源)、无重复定义
2. prev_posterior 前向滤波纯函数 + 熵产出正确
3. 6 states 映射纯函数
"""
import sys, os, math
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))


def test_transitions_single_source_row_sums_one():
    """TRANSITIONS 每行和=1.0; 模块级导入与类级引用指向同一对象."""
    import chiguo_bayesian as m
    from chiguo_bayesian import UserStateEstimator
    # 单源: 模块级 TRANSITIONS 与类级指向同一对象或相等
    mod_trans = getattr(m, "TRANSITIONS", None)
    cls_trans = UserStateEstimator.TRANSITIONS
    if mod_trans is not None:
        assert mod_trans is cls_trans or mod_trans == cls_trans, "TRANSITIONS 非单一事实源 (模块与类不一致)"
    for s in UserStateEstimator.STATES:
        row = cls_trans[s]
        assert abs(sum(row.values()) - 1.0) < 1e-9, f"{s} 行和≠1: {sum(row.values())}"
        assert set(row.keys()) == set(UserStateEstimator.STATES)


def test_no_duplicate_transitions_definition():
    """源码中 TRANSITIONS 字面量仅一处定义 (去重)."""
    import re
    src = open("chiguo_bayesian.py", encoding="utf-8").read()
    # 去重后应只有一处字面量定义 "TRANSITIONS ... = {" (模块级单一事实源)
    count = len(re.findall(r"^\s*TRANSITIONS[^=]*=\s*\{", src, re.MULTILINE))
    assert count == 1, f"TRANSITIONS 重复定义: 发现 {count} 处"


def test_forward_filter_pure_function():
    """前向滤波为纯函数: forward_filter(prev, config) 无副作用, 输入相同输出相同."""
    from chiguo_bayesian import forward_filter, TRANSITIONS, UserState
    prev = {s: 1/6 for s in UserState.values()}
    prev["chatting"] = 1.0
    for k in list(prev.keys()):
        if k != "chatting":
            prev[k] = 0.0
    r1 = forward_filter(prev, TRANSITIONS, config={})
    r2 = forward_filter(prev, TRANSITIONS, config={})
    assert r1 == r2, "纯函数应幂等"
    assert abs(sum(r1.values()) - 1.0) < 1e-9
    # 无副作用: 输入未被修改
    assert prev["chatting"] == 1.0


def test_posterior_update_pure_function():
    """posterior_update 为纯函数: 输入 prior+likelihood → posterior 不依赖全局."""
    from chiguo_bayesian import posterior_update, UserState
    prior = {s: 1/6 for s in UserState.values()}
    likelihood = {s: 0.2 for s in UserState.values()}
    likelihood[UserState.CHATTING.value] = 0.9
    p1 = posterior_update(prior, likelihood)
    p2 = posterior_update(prior, likelihood)
    assert p1 == p2
    assert abs(sum(p1.values()) - 1.0) < 1e-9


def test_entropy_output_correct():
    """compute_entropy 正确: 均匀 6 状态熵=log2(6); 确定性熵=0."""
    from chiguo_bayesian import compute_entropy, UserState
    uniform = {s: 1/6 for s in UserState.values()}
    e_uniform = compute_entropy(uniform)
    assert abs(e_uniform - math.log2(6)) < 1e-9, f"uniform entropy {e_uniform}"
    peaked = {s: 0.0 for s in UserState.values()}
    peaked[UserState.SLEEPING.value] = 1.0
    e_peaked = compute_entropy(peaked)
    assert abs(e_peaked - 0.0) < 1e-9


def test_user_state_typed_enum():
    """6 states 为类型化枚举/NamedTuple, 纯函数映射."""
    from chiguo_bayesian import UserState
    assert len(list(UserState)) == 6
    assert hasattr(UserState, "CHATTING")
    vals = {m.value for m in UserState}
    assert vals == {"chatting", "browsing", "busy", "sleeping", "away", "needs_care"}


def test_state_enum_utility_mapping_pure():
    """UTILITY 映射为纯函数 state_utility(state) → float, 无全局依赖."""
    from chiguo_bayesian import state_utility, UserState
    assert abs(state_utility(UserState.SLEEPING) - 0.0) < 1e-9
    assert abs(state_utility(UserState.NEEDS_CARE) - 0.9) < 1e-9
    assert abs(state_utility("browsing") - 0.7) < 1e-9  # str 也兼容
