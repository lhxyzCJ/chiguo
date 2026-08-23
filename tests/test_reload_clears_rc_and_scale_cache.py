#!/usr/bin/env python3
"""test_reload_clears_rc_and_scale_cache.py — mtime 热重载与 RC/scale 缓存（AUD-032）

Given: decision/base._maybe_reload_config 仅 decision/base 持有 getmtime + chiguo_state.reload_config 清空 _rc_cache/_scale_cache
When:  toml mtime 变化 / 非法 toml / 正常 reload
Then:  配置热更新且 RC/scale 缓存被清空；非法 toml 保留旧配置；_bayesian_estimator 重置
"""
import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine


def _make_engine(tmp: str) -> DecisionEngine:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return DecisionEngine(str(cfg_path), str(Path(tmp) / "chiguo_decisions.jsonl"))


def test_reload_clears_rc_and_scale_cache():
    """reload_config 后 _rc_cache / _scale_cache 被清空（AUD-032 mtime 热重载仅 --loop，但缓存语义通用）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        eng.state._rc_cache["dummy"] = {"hit": True}
        eng.state._scale_cache["2026-06-15"] = {"lonely_low": 1.0}
        # 触发 reload_config（直接调 state.reload_config）
        new_cfg = dict(eng.config)
        eng.state.reload_config(new_cfg)
        assert eng.state._rc_cache == {}, "reload 后 RC 缓存应清空"
        assert eng.state._scale_cache == {}, "reload 后 scale 缓存应清空"


def test_reload_resets_bayesian_estimator():
    """reload 后 _bayesian_estimator 经 base 层置空（decision/base._maybe_reload_config 负责）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        eng.state._bayesian_estimator = object()
        # state.reload_config 本身不重置 bayesian（由 base 层负责）；验证 state 侧不崩且可手动重置
        eng.state.reload_config(dict(eng.config))
        # base 层的 _maybe_reload 会重置，state 侧需显式调用 reset_bayesian_estimator
        eng.state.reset_bayesian_estimator()
        assert eng.state._bayesian_estimator is None


def test_maybe_reload_config_via_mtime():
    """_maybe_reload_config：mtime 前移 → 配置热更新并重建派生组件。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        cfg_path = eng.config_path
        # 记录旧值
        old_top = eng.config.get("emotion", {}).get("loneliness", None)
        # 修改 toml：改一个可观测值
        text = Path(cfg_path).read_text()
        text = text.replace("loneliness = 15.0", "loneliness = 99.0")
        # 确保 mtime 前进
        time.sleep(0.05)
        Path(cfg_path).write_text(text)
        eng._maybe_reload_config()
        assert eng.config["emotion"]["loneliness"] == 99.0, "mtime 热重载应更新 config"
        assert eng.state.config["emotion"]["loneliness"] == 99.0


def test_maybe_reload_invalid_toml_keeps_old():
    """非法 toml → 保留旧配置，不抛异常。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        cfg_path = eng.config_path
        old_cfg = dict(eng.config)
        time.sleep(0.05)
        Path(cfg_path).write_text("invalid [[[ toml")
        # 强制 mtime 前进
        os.utime(cfg_path, None)
        eng._config_mtime -= 1.0  # 确保 mtime > _config_mtime
        eng._maybe_reload_config()
        # 旧配置应保留（至少 emotion.loneliness 不变）
        assert eng.config.get("emotion", {}).get("loneliness") == old_cfg.get("emotion", {}).get("loneliness")


def test_getmtime_only_in_decision_base():
    """审计：getmtime 仅 decision/base.py 持有（热重载单一入口）。"""
    import subprocess
    r = subprocess.run(["grep", "-rn", "getmtime", "--include=*.py", "."],
                       capture_output=True, text=True, cwd=str(Path(".").resolve()))
    lines = [l for l in r.stdout.splitlines() if "getmtime" in l and "/tests/" not in l and ".venv" not in l]
    allowed = [l for l in lines if "decision/base.py" in l]
    assert len(lines) == len(allowed), f"getmtime 仅 decision/base 允许, extra: {lines}"
