#!/usr/bin/env python3
"""test_pending_contract.py — pending 话题 20/48h 契约 (Issue #379)。

锁定 PendingMixin 薄包装语义（委托 chiguo_pending 纯函数）：
  1. cap=20：超 20 条只保留最新 20 条（add / prune / _cap 三入口一致）；
  2. 48h：prune 移除超 48h 条目与已尝试条目；
  3. resolve：指定 topic 移除对应条目；未指定移除最旧一条。
"""

import os
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState


def _make_state(tmp: str) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return ChiguoState(cfg)


def test_pending_cap_20(tmp_path):
    """add 超 20 条只保留最新 20 条；_cap_pending_topics 同语义。"""
    s = _make_state(str(tmp_path))
    now = datetime.now(CST)
    for i in range(25):
        s.add_pending_topic(f"话题{i}", now)
    assert len(s.pending_topics) == 20
    assert s.pending_topics[0]["topic"] == "话题5"
    assert s.pending_topics[-1]["topic"] == "话题24"
    s.pending_topics.extend([{"topic": f"extra{i}", "created_at": now.isoformat()} for i in range(10)])
    s._cap_pending_topics()
    assert len(s.pending_topics) == 20
    print("  OK test_pending_cap_20")


def test_pending_prune_48h_and_attempted(tmp_path):
    """prune 移除超 48h 条目与已尝试条目，保留新鲜未尝试条目。"""
    s = _make_state(str(tmp_path))
    now = datetime.now(CST)
    old = (now - timedelta(hours=49)).isoformat()
    fresh = now.isoformat()
    s.pending_topics = [
        {"topic": "过期", "created_at": old, "attempted": False},
        {"topic": "已尝试", "created_at": fresh, "attempted": True},
        {"topic": "新鲜", "created_at": fresh, "attempted": False},
    ]
    s.prune_pending_topics(now)
    assert [t["topic"] for t in s.pending_topics] == ["新鲜"]
    print("  OK test_pending_prune_48h_and_attempted")


def test_pending_resolve(tmp_path):
    """resolve 指定 topic 移除对应条目；未指定移除最旧一条。"""
    s = _make_state(str(tmp_path))
    now = datetime.now(CST)
    for t in ("A", "B", "C"):
        s.add_pending_topic(t, now)
    s.resolve_pending_topic("B", now)
    assert [t["topic"] for t in s.pending_topics] == ["A", "C"]
    s.resolve_pending_topic(None, now)
    assert [t["topic"] for t in s.pending_topics] == ["C"]
    print("  OK test_pending_resolve")
