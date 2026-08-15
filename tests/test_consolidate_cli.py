#!/usr/bin/env python3
"""test_consolidate_cli.py — `--consolidate` CLI 全路径测试（C1）

覆盖: exit 0/1 约定、后端无 consolidate → exit 1、consolidate 异常 → 结构化错误
（不裸 traceback）、报告 JSON 形状（action/demoted_ids/expired_ids）、隐私剥离
（stdout 报告的 demoted/expired 行不含 text 字段）。经 DecisionEngine.cli_consolidate
注入 fake state.memory_bridge 全路径驱动，零 LLM、零网络、不触真实 mem0/qdrant。
"""

import io
import json
import os
import re
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import DecisionEngine  # noqa: E402


def _engine(tmp: str) -> DecisionEngine:
    """临时 toml + 独立 decision log 的引擎（mem0 指向不存在的路径 + 禁用）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(tmp) / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{Path(tmp) / "no_history.db"}"', src)
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return DecisionEngine(str(cfg_path), str(Path(tmp) / "decisions.jsonl"))


class ReportBackend:
    """consolidate() 返回含 text 字段报告的 fake 后端（校验隐私剥离）。"""

    def __init__(self, ok=True):
        self._ok = ok

    def consolidate(self, sim_threshold=None, min_importance=None, max_age_hours=None):
        return {
            "available": True, "ok": self._ok, "dry_run": False,
            "scanned": 3,
            "demoted": [{"id": "b", "text": "哥哥喜欢喝美式咖啡", "importance": 0.25}],
            "expired": [{"id": "old", "text": "多年前的琐事", "importance": 0.1}],
            "kept": [{"id": "a", "text": "保留的高重要性记忆", "importance": 0.9}],
            "demoted_ids": ["b"], "expired_ids": ["old"],
        }


class RaisingBackend:
    """consolidate() 抛异常（配置/后端事故）→ 应结构化错误 exit 1。"""

    def consolidate(self, sim_threshold=None, min_importance=None, max_age_hours=None):
        raise RuntimeError("qdrant 不可用")


class NoConsolidateBackend:
    """无 consolidate 属性的后端（mem0 缺失/被禁用等）→ exit 1 明确报错。"""


def test_cli_no_backend_returns_1():
    """后端不支持 consolidate → exit 1 + JSON ok:False（明确报错非静默）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        eng.state.memory_bridge = NoConsolidateBackend()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = eng.cli_consolidate()
        out = json.loads(buf.getvalue())
        assert rc == 1, f"无 consolidate 后端应 exit 1, got {rc}"
        assert out["action"] == "consolidate" and out["ok"] is False
        assert "不支持" in out["error"]
    print("  OK test_cli_no_backend_returns_1")


def test_cli_report_ok_strips_text():
    """报告含 text 的记忆行 → exit 0 + demoted/expired/kept 行剥离 text 字段
    （隐私：stdout 落日志不泄露私聊内容），demoted_ids/expired_ids 仍在。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        eng.state.memory_bridge = ReportBackend(ok=True)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = eng.cli_consolidate()
        out = json.loads(buf.getvalue())
        assert rc == 0, f"ok=True 应 exit 0, got {rc}"
        assert out["action"] == "consolidate" and out["ok"] is True
        assert out["demoted_ids"] == ["b"] and out["expired_ids"] == ["old"]
        for k in ("demoted", "expired", "kept"):
            for row in out[k]:
                assert "text" not in row, f"{k} 行不应含 text 字段: {row}"
        assert out["demoted"][0]["id"] == "b"  # 非 text 字段保留
    print("  OK test_cli_report_ok_strips_text")


def test_cli_report_not_ok_returns_1():
    """报告 ok=False（后端不可用等）→ exit 1（cron 可感知失败）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        eng.state.memory_bridge = ReportBackend(ok=False)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = eng.cli_consolidate()
        out = json.loads(buf.getvalue())
        assert rc == 1, f"ok=False 应 exit 1, got {rc}"
        assert out["ok"] is False
    print("  OK test_cli_report_not_ok_returns_1")


def test_cli_consolidate_raises_structured_error():
    """consolidate() 抛异常 → exit 1 + JSON error 字段（不裸 traceback）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        eng.state.memory_bridge = RaisingBackend()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = eng.cli_consolidate()
        out = json.loads(buf.getvalue())
        assert rc == 1, f"异常应 exit 1, got {rc}"
        assert out["ok"] is False and "consolidate failed" in out["error"]
        assert "Traceback" not in buf.getvalue()
    print("  OK test_cli_consolidate_raises_structured_error")


def test_cli_config_threshold_string_passthrough():
    """cli_consolidate 把 toml 阈值传给后端（字符串也透传，由后端 _finite_float 兜底）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _engine(td)
        seen = {}

        class Capturing(ReportBackend):
            def consolidate(self, sim_threshold=None, min_importance=None,
                            max_age_hours=None):
                seen.update(sim_threshold=sim_threshold, min_importance=min_importance,
                            max_age_hours=max_age_hours)
                return super().consolidate()

        eng.state.memory_bridge = Capturing()
        # 手改 toml 为字符串阈值 → 后端应收到（coerce 在 Mem0Backend 内，见 test_memory_consolidate）
        eng.config["memory"]["consolidate_sim_threshold"] = "0.85"
        eng.config["memory"]["consolidate_min_importance"] = "0.3"
        eng.config["memory"]["consolidate_max_age_hours"] = "720.0"
        with redirect_stdout(io.StringIO()):
            rc = eng.cli_consolidate()
        assert rc == 0
        assert seen["sim_threshold"] == "0.85", f"字符串阈值应透传: {seen}"
    print("  OK test_cli_config_threshold_string_passthrough")
