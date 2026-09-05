#!/usr/bin/env python3
"""test_mro_contract.py — DecisionEngine/ChiguoState MRO 菱形继承契约 (Issue #381)。

背景：DecisionEngine 由 4 个 mixin 组合而成且全部汇入 DecisionEngineBase，
构成菱形继承；MRO 顺序即组合顺序，改序即改行为。现状恰好无同名方法冲突，
新增 mixin 时静默覆盖会改行为且难排查。本契约锁定两点：
  1. MRO 顺序恒等（改组合顺序 / 新增 mixin 即失败，须显式更新契约）；
  2. 层级内无非 dunder 同名方法覆盖（新增冲突方法即失败）。
另附带 decision.context 的 datetime 回归：_build_context 注解引用 datetime，
缺 import 时 get_type_hints 即 NameError（曾阻塞本 issue 复现命令）。
""".strip()

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from decision.engine import DecisionEngine  # noqa: E402
from chiguo_state import ChiguoState  # noqa: E402
from decision.context import ContextMixin  # noqa: E402

# MRO 顺序契约：改组合顺序 / 新增 mixin 即失败，须显式更新此处
_EXPECTED_MRO = {
    DecisionEngine: ("DecisionEngine", "DecisionCoreMixin", "IdleMixin",
                     "ContextMixin", "AccountingMixin", "LoopSenderMixin",
                     "DecisionEngineBase", "object"),
    ChiguoState: ("ChiguoState", "ScheduleMixin", "EmotionMixin",
                  "InteractionMixin", "object"),
}


def _public_names(cls):
    """类自身定义的非 dunder 成员名（3.14 的 __firstlineno__ 等编译器 dunder 除外）。"""
    return {n for n in vars(cls)
            if not (n.startswith("__") and n.endswith("__"))}


def _check_mro_contract(cls):
    actual = tuple(c.__name__ for c in cls.__mro__)
    assert actual == _EXPECTED_MRO[cls], (
        f"{cls.__name__} MRO 漂移：{actual}，预期 {_EXPECTED_MRO[cls]}"
        " —— 组合顺序变更须显式更新本契约"
    )
    seen = {}
    for klass in cls.__mro__:
        if klass is object:
            continue
        for name in _public_names(klass):
            assert name not in seen, (
                f"{cls.__name__} 覆盖冲突：{klass.__name__}.{name} 被"
                f" {seen[name]} 遮蔽 —— 同名方法静默改行为，须改名或显式 super() 链"
            )
            seen[name] = klass.__name__


def test_decision_engine_mro_contract():
    """DecisionEngine 菱形 MRO 顺序 + 零覆盖冲突。"""
    _check_mro_contract(DecisionEngine)
    print("  OK test_decision_engine_mro_contract")


def test_chiguo_state_mro_contract():
    """ChiguoState 线性 MRO 顺序 + 零覆盖冲突。"""
    _check_mro_contract(ChiguoState)
    print("  OK test_chiguo_state_mro_contract")


def test_context_datetime_import():
    """decision.context 须绑定 datetime（_build_context 注解引用，缺则 NameError）。"""
    import datetime as dt

    assert ContextMixin.__module__ and getattr(
        sys.modules["decision.context"], "datetime", None) is dt.datetime
    import typing
    typing.get_type_hints(ContextMixin._build_context)  # 缺 import 时此处抛 NameError
    print("  OK test_context_datetime_import")
