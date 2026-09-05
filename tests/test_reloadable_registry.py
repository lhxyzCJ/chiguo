#!/usr/bin/env python3
"""test_reloadable_registry.py — 热重载重建注册表（#394 P2-13）。

Given: decision/base 曾在 _maybe_reload_config 内逐项手写重建
       （state.reload_config / schedule.reload / PlayProofProvider /
        composer.schedule_facade / topic_picker / composer / bayesian 重置），
       新增 config 派生组件时易遗漏
When:  RELOADABLE_COMPONENTS 注册表收敛重建清单，新增组件只需 register()
Then:  注册表含全部重建函数；新注册的组件在 _maybe_reload_config 时被执行；
       存量重建语义（config 更新 + bayesian 重置）保持不变
"""
import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from decision.base import (
    RELOADABLE_COMPONENTS,
    register_reloadable,
)
from chiguo_daemon import DecisionEngine


def _make_engine(tmp: str) -> DecisionEngine:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return DecisionEngine(str(cfg_path), str(Path(tmp) / "chiguo_decisions.jsonl"))


def test_registry_contains_expected_rebuilds():
    """注册表收敛全部重建项：state / schedule / play_proof / picker / composer / bayesian。"""
    names = {fn.__name__ for fn in RELOADABLE_COMPONENTS}
    assert names == {
        "_reload_state",
        "_reload_schedule",
        "_reload_play_proof",
        "_reload_topic_picker",
        "_reload_composer",
        "_reload_bayesian",
    }, f"注册表缺项或多余: {names}"


def test_register_reloadable_returns_fn():
    """register() 返回原函数（支持装饰器用法），且注册后可移除（不污染全局注册表）。"""
    def _probe(engine):
        pass
    ret = register_reloadable(_probe)
    assert ret is _probe
    assert _probe in RELOADABLE_COMPONENTS
    # 清理：本用例的探针不留给后续用例
    RELOADABLE_COMPONENTS.remove(_probe)


def test_newly_registered_component_runs_on_reload():
    """新注册的组件在 _maybe_reload_config 时被执行（新增派生组件只需 register()）。"""
    calls = []
    def _probe(engine):
        calls.append(engine.config.get("emotion", {}).get("loneliness"))
    register_reloadable(_probe)
    try:
        with tempfile.TemporaryDirectory() as td:
            eng = _make_engine(td)
            text = Path(eng.config_path).read_text()
            text = text.replace("loneliness = 15.0", "loneliness = 99.0")
            time.sleep(0.05)
            Path(eng.config_path).write_text(text)
            eng._maybe_reload_config()
            assert calls == [99.0], f"新注册组件应在重载时执行且读到新 config: {calls}"
            # 存量语义保持：config 更新 + bayesian 重置
            assert eng.config["emotion"]["loneliness"] == 99.0
            assert eng.state._bayesian_estimator is None
    finally:
        RELOADABLE_COMPONENTS.remove(_probe)
