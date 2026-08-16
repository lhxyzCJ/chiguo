#!/usr/bin/env python3
"""test_memory_governance.py — R17 记忆库治理回归测试（Issue #336）

覆盖:
  1. consolidate 过期可达（F-A21-001）：
     超龄（>720h）+ 缺 importance metadata 的行 → 应进 expired；
     显式 importance < min_importance 的超龄行 → 过期（对照，不回归）。
  2. autowrite 24h 文本 hash 去重（F-A21-002）：
     同文本 24h 内二次写入 → 跳过；不同文本 → 写入。
  3. 写链故障可感知（F-RT-017 修正）：add_messages 失败 → add_fail_count
     递增并暴露进 stats()（供 monitor 读取）。
全部零 LLM、零网络（FakeMem0/FakeBridge 注入，CHIGUO_MEM0_DISABLED=1 + tempdir 隔离，
不触真实 data/mem0/）。
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from memory import Mem0Backend  # noqa: E402


def _now() -> datetime:
    return datetime.now(CST)


# ── FakeMem0：可注入 add 失败（写链故障模拟）+ delete/update 记录 ──
class FakeMem0:
    def __init__(self, results, fail_add: bool = False):
        self._results = list(results)
        self.fail_add = fail_add
        self.add_calls = []
        self.deleted: list[str] = []

    def search(self, query, filters=None, top_k=10):
        return {"results": list(self._results)}

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self._results)}

    def add(self, messages, user_id=None, metadata=None):
        if self.fail_add:
            raise RuntimeError("LLM 事实提取端点故障（opencode/model）")
        self.add_calls.append({"messages": messages, "user_id": user_id,
                               "metadata": metadata})
        return {"results": []}

    def delete(self, memory_id):
        self.deleted.append(memory_id)


def _mem0_result(text, age_hours=1.0, mem_id=None, importance=None, created=None):
    """构造 mem0 原始 result 行。

    importance=None → metadata 不含 importance（模拟 mem0 无 importance 概念，
    无 metadata importance 信息）；显式数值 → 携带 importance。
    """
    ts = _now().timestamp() - age_hours * 3600
    created = created or datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    meta = {"category": "conversation"}
    if importance is not None:
        meta["importance"] = importance
    return {"id": mem_id or f"m-{abs(hash(text)) % 100000}",
            "memory": text, "hash": "h", "metadata": meta,
            "created_at": created, "updated_at": created, "user_id": "chiguo"}


def _fake_backend(results, tmp: Path, fail_add=False, **cls_kwargs) -> Mem0Backend:
    b = Mem0Backend(qdrant_path=str(tmp / "qdrant"),
                    history_db=str(tmp / "h.db"), **cls_kwargs)
    b._available = True
    b._last_probe = time.time() + 3600  # 缓存恒命中，永不真实探测
    b._m = FakeMem0(results, fail_add=fail_add)
    return b


# ════ 1. consolidate 过期可达（F-A21-001）════

def test_consolidate_expires_overage_missing_importance_metadata():
    """[R17] 超龄（900h>720h）+ 缺 importance metadata（_row 曾固定回退 0.5）
    → 必须进 expired。修复前红：0.5 ≥ min_importance 使过期条件永不满足。"""
    now = _now()
    rows = [
        _mem0_result("多年前的无标签琐事", age_hours=900, mem_id="old-nometa",
                     importance=None),  # 缺 importance metadata
        _mem0_result("最近的新记忆", age_hours=1, mem_id="new-nometa",
                     importance=None),  # 缺 importance 但不超龄 → 保留
    ]
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend(rows, Path(td))
        rep = b.consolidate(max_age_hours=720.0, min_importance=0.3)
        assert rep["ok"] and rep["available"], rep
        assert "old-nometa" in rep["expired_ids"], \
            f"超龄+无 importance 信息的行应过期, got expired_ids={rep['expired_ids']}"
        assert "old-nometa" in b._m.deleted, \
            f"过期行应被 delete, got deleted={b._m.deleted}"
        assert "new-nometa" not in rep["expired_ids"], \
            f"不超龄不应过期, got {rep['expired_ids']}"
    print("  OK test_consolidate_expires_overage_missing_importance_metadata")


def test_consolidate_explicit_low_importance_still_expires():
    """[R17] 显式 importance=0.2（<0.3）的超龄行 → 过期（对照，语义不回归）。"""
    now = _now()
    rows = [
        _mem0_result("多年前的琐事", age_hours=900, mem_id="old-low",
                     importance=0.2),
        _mem0_result("超龄但重要", age_hours=900, mem_id="old-high",
                     importance=0.9),  # importance 高 → 不过期
    ]
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend(rows, Path(td))
        rep = b.consolidate(max_age_hours=720.0, min_importance=0.3)
        assert "old-low" in rep["expired_ids"], \
            f"显式低 importance 超龄行应过期, got {rep['expired_ids']}"
        assert "old-high" not in rep["expired_ids"], \
            f"高 importance 行不应过期, got {rep['expired_ids']}"
    print("  OK test_consolidate_explicit_low_importance_still_expires")


def test_consolidate_explicit_recent_low_importance_kept():
    """[R17] 显式低 importance 但不超龄 → 保留（不过期，误删防御）。"""
    rows = [
        _mem0_result("最近的低重要度闲聊", age_hours=1, mem_id="recent-low",
                     importance=0.1),
    ]
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend(rows, Path(td))
        rep = b.consolidate(max_age_hours=720.0, min_importance=0.3)
        assert rep["expired_ids"] == [], f"不超龄低 importance 不应过期, got {rep}"
    print("  OK test_consolidate_explicit_recent_low_importance_kept")


# ════ 2. autowrite 24h 文本 hash 去重（F-A21-002）════

class WritingBridge:
    """记忆桥替身：available=True，记录 add_messages 调用。"""

    available = True

    def __init__(self):
        self.add_calls = []

    def add_messages(self, messages, metadata=None):
        self.add_calls.append({"messages": messages, "metadata": metadata})
        return True


def _engine(bridge: WritingBridge, mem_cfg=None):
    eng = SimpleNamespace(
        config={"memory": mem_cfg or {}},
        state=SimpleNamespace(memory_bridge=bridge),
    )
    eng.recent_sent_texts = lambda n=1: []
    return eng


def _autowrite(eng, text: str):
    from chiguo_daemon import DecisionEngine
    DecisionEngine._mem0_autowrite(eng, text)


def test_autowrite_same_text_within_24h_skipped():
    """[R17] 同文本 24h 内二次写入 → 跳过（不重复增长 messages 表）。
    修复前红：无去重，二次写入会再 add。"""
    bridge = WritingBridge()
    eng = _engine(bridge)
    _autowrite(eng, "哥哥今天工作累吗？去喝水吧")
    assert len(bridge.add_calls) == 1, "首次应写入"
    _autowrite(eng, "哥哥今天工作累吗？去喝水吧")
    assert len(bridge.add_calls) == 1, \
        f"同文本 24h 内二次写入应被跳过, got add_calls={len(bridge.add_calls)}"
    print("  OK test_autowrite_same_text_within_24h_skipped")


def test_autowrite_different_text_written():
    """[R17] 不同文本 → 各自写入（不误伤正常记忆提取）。"""
    bridge = WritingBridge()
    eng = _engine(bridge)
    _autowrite(eng, "哥哥今天工作累吗？去喝水吧")
    _autowrite(eng, "我们周末去苏州看园林吧")
    assert len(bridge.add_calls) == 2, \
        f"不同文本应各自写入, got add_calls={len(bridge.add_calls)}"
    texts = [" ".join(m.get("content", "") for m in c["messages"]) for c in bridge.add_calls]
    assert any("去喝水" in t for t in texts)
    assert any("苏州" in t for t in texts)
    print("  OK test_autowrite_different_text_written")


def test_autowrite_dedup_window_expiry():
    """[R17] 超过 24h 去重窗口后同文本再次写入应放行（窗口语义）。"""
    import ops.engine_ops as eo
    bridge = WritingBridge()
    eng = _engine(bridge)

    _autowrite(eng, "好久不见的问候复用文本")
    assert len(bridge.add_calls) == 1
    # 让已记录 hash 的写入时间退到 25h 前 → 应放行再写
    for k in list(getattr(eo, "_mem0_autowrite_hashes", {})):
        if k in getattr(eng, "_mem0_autowrite_hashes", {}):
            h = eng._mem0_autowrite_hashes[k]
            # 覆盖时间戳为超过窗口
            eng._mem0_autowrite_hashes[k] = (
                datetime.now(CST) - timedelta(hours=25)
            ).isoformat()
    _autowrite(eng, "好久不见的问候复用文本")
    assert len(bridge.add_calls) == 2, \
        f"超 24h 窗口后同文本应再写入, got add_calls={len(bridge.add_calls)}"
    print("  OK test_autowrite_dedup_window_expiry")


# ════ 3. 写链故障可感知（F-RT-017 修正）════

def test_add_failure_exposes_count_in_stats():
    """[R17] add_messages 失败 → add_fail_count 递增并暴露进 stats()
    （供 monitor 感知 LLM 写链故障）。修复前红：无 add_fail_count 字段。"""
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([], Path(td), fail_add=True)
        assert b.add_messages([{"role": "user", "content": "写链故障"}])
        # 第一次失败
        assert b.add_messages([{"role": "user", "content": "写链故障2"}]) is False
        assert b.add_messages([{"role": "user", "content": "写链故障3"}]) is False
        s = b.stats()
        assert "add_fail_count" in s, \
            f"stats() 应暴露 add_fail_count, got keys={sorted(s.keys())}"
        assert s["add_fail_count"] == 2, \
            f"两次 add 失败应累计 add_fail_count=2, got {s['add_fail_count']}"
        assert s.get("last_error") and s["last_error"][1] == "add", \
            f"last_error 应记录 add 失败, got {s.get('last_error')}"
        assert b._add_fail_count == 2
        # 不可用分支也应暴露（恒 0 或当前值），不破 stats 形状
        b._available = False
        s2 = b.stats()
        assert "add_fail_count" in s2
    print("  OK test_add_failure_exposes_count_in_stats")


def test_add_success_does_not_increment_fail_count():
    """[R17] add 成功不累计 add_fail_count（只记失败）。"""
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([], Path(td), fail_add=False)
        assert b.add_messages([{"role": "user", "content": "写链正常"}]) is True
        assert b.add_fail_count == 0, \
            f"成功 add 不应计数, got add_fail_count={b.add_fail_count}"
    print("  OK test_add_success_does_not_increment_fail_count")
