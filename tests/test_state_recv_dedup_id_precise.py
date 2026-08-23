#!/usr/bin/env python3
"""test_state_recv_dedup_id_precise.py — 盲区7 recv_dedup 450s 边界（AUD-032）

Given: ops/engine_ops.RECV_DEDUP_WINDOW_S=450 + is_recv_upgrade(recv_id 优先 / text_sha+窗口回退)
When:  同 id 二次升级 / 跨 450s 边界 / 不同文本 / 无 recv_id 回退
Then:  精确去重与窗口语义均符合 449s 内升级、451s 外重放
"""
import hashlib
import os
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_daemon import DecisionEngine
from ops.analysis_ops import is_recv_upgrade

WINDOW_S = 450


def _make_engine(tmp: str):
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    os.environ["CHIGUO_MEM0_AUTOWRITE"] = "0"
    eng = DecisionEngine(str(cfg_path), str(Path(tmp) / "chiguo_decisions.jsonl"))
    return eng


def test_is_recv_upgrade_recv_id_precise():
    """同 recv_id → 升级（不看窗口）；不同 id → 按 text_sha+窗口。"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    dedup = {"text_sha": "abc", "at": now.isoformat(), "analysis": False, "recv_id": "rid-1"}
    # 同 id 即使 text_sha 不同也升级（recv_id 优先）
    assert is_recv_upgrade({"mood": "low"}, dedup, "different-sha", "rid-1", now, WINDOW_S) is True
    # 不同 id → 按 text_sha+窗口
    assert is_recv_upgrade({"mood": "low"}, dedup, "abc", "rid-2", now + timedelta(seconds=10), WINDOW_S) is True
    assert is_recv_upgrade({"mood": "low"}, dedup, "different", "rid-2", now + timedelta(seconds=10), WINDOW_S) is False


def test_window_inclusive_449_upgrades():
    """449s 内 → 升级（text_sha 命中 + 窗口内）。"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    text = "hello world"
    sha = hashlib.sha256(text.encode()).hexdigest()
    dedup = {"text_sha": sha, "at": now.isoformat(), "analysis": False, "recv_id": None}
    later = now + timedelta(seconds=449)
    assert is_recv_upgrade({"mood": "low"}, dedup, sha, None, later, WINDOW_S) is True


def test_window_exclusive_451_no_upgrade():
    """451s 外 → 不升级（窗口外视为重发）。"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    text = "hello world"
    sha = hashlib.sha256(text.encode()).hexdigest()
    dedup = {"text_sha": sha, "at": now.isoformat(), "analysis": False, "recv_id": None}
    later = now + timedelta(seconds=451)
    assert is_recv_upgrade({"mood": "low"}, dedup, sha, None, later, WINDOW_S) is False


def test_upgrade_sets_analysis_true_persists():
    """升级后落盘的 recv_dedup.analysis=True；再次同文本不重复升级。"""
    import time
    from unittest import mock
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        rid = "uuid-1234"
        text = "test dedup window"
        # 第一步：无分析入 dedup
        eng.record_user_message(text, analysis_json=None, recv_id=rid)
        d = eng.state.cooldown.recv_dedup
        assert d["recv_id"] == rid and d["analysis"] is False
        # 第二步：同 id 补分析 → 升级，落盘 analysis=True
        eng2 = _make_engine(td)
        # 手动把第一条的 recv_dedup 恢复（跨进程持久化验证）
        # 重新构造 engine 已从磁盘 _load，直接补分析
        eng2.state._load()
        assert eng2.state.cooldown.recv_dedup["recv_id"] == rid
        eng2.record_user_message(text, analysis_json='{"mood":"low","intensity":0.5}', recv_id=rid)
        # 升级后应为 analysis=True
        eng2.state._load()
        assert eng2.state.cooldown.recv_dedup["analysis"] is True
        # 第三步：再次同文本同 id（已升级过）→ 不再升级（analysis 已 True）
        assert is_recv_upgrade({"mood": "low"}, eng2.state.cooldown.recv_dedup,
                               hashlib.sha256(text.encode()).hexdigest(), rid,
                               datetime.now(CST), WINDOW_S) is False


def test_same_recv_id_no_double_count_e2e():
    """同 id 二次不重复入 event_timestamps / 不重复情绪骤降（通过 record 路径间接验证不抛）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _make_engine(td)
        rid = "same-id-no-double"
        text = "dedup e2e no double count"
        eng.record_user_message(text, analysis_json=None, recv_id=rid)
        n1 = len(eng.state.cooldown.event_timestamps)
        # 同 id 补分析 → 升级路径不走 on_user_message，不新增 event
        eng.record_user_message(text, analysis_json='{"mood":"low"}', recv_id=rid)
        n2 = len(eng.state.cooldown.event_timestamps)
        assert n2 == n1, f"升级不应新增 event: {n1}→{n2}"


def test_no_recv_id_fallback_text_sha_window():
    """无 recv_id → 回退 text_sha+窗口：窗口内升级，窗口外重放。"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    text = "fallback window text"
    sha = hashlib.sha256(text.encode()).hexdigest()
    dedup = {"text_sha": sha, "at": now.isoformat(), "analysis": False, "recv_id": None}
    assert is_recv_upgrade({"mood": "low"}, dedup, sha, None, now + timedelta(seconds=100), WINDOW_S) is True
    assert is_recv_upgrade({"mood": "low"}, dedup, sha, None, now + timedelta(seconds=500), WINDOW_S) is False
