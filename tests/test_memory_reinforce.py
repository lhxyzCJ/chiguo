#!/usr/bin/env python3
"""test_memory_reinforce.py — C2 Ebbinghaus 复习强化测试

覆盖: note_recalled 计数/默认关闭恒等、_effective_importance 加成与封顶、
_persist_recall 经 FakeMem0 update_memory 写回 recall_count、
search_with_forgetting / random_memory_with_forgetting 召回即强化、
_apply_forgetting 中被召回记忆 _score 更强。
全部零 LLM、零网络（不触真实 ollama/qdrant）。
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from memory import Mem0Backend  # noqa: E402
from memory.base import MemoryBackend  # noqa: E402


class FakeMem0:
    """mem0 Memory 最小模拟 + update_memory 记录（C2 写回断言）。"""

    def __init__(self, results):
        self._results = list(results)
        self.updated: list[tuple] = []

    def search(self, query, filters=None, top_k=10):
        return {"results": list(self._results)}

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self._results)}

    def add(self, messages, user_id=None, metadata=None):
        return {"results": []}

    def update_memory(self, memory_id, data):
        self.updated.append((memory_id, data))


def _row(text="哥哥喜欢喝美式咖啡", importance=0.9, mem_id="m1",
         age_hours=1.0):
    ts = datetime.now(CST).timestamp() - age_hours * 3600
    return {"id": mem_id, "memory": text, "hash": "h",
            "metadata": {"category": "preferences", "importance": importance},
            "created_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "user_id": "chiguo"}


def _backend(results, tmp: Path, **kw) -> Mem0Backend:
    b = Mem0Backend(qdrant_path=str(tmp / "qdrant"), history_db=str(tmp / "h.db"),
                    **kw)
    b._available = True
    b._last_probe = time.time() + 3600  # 缓存恒命中，永不真实探测
    b._m = FakeMem0(results)
    return b


# ── note_recalled 计数与默认关闭恒等 ─────────────────────

def test_note_recalled_disabled_noop():
    """reinforce 默认关闭 → note_recalled 返回 0、不记 count、无写回。"""
    b = MemoryBackend()
    assert b.note_recalled(["m1", "m2"]) == 0
    assert b._recall_counts_dict() == {}
    print("  OK test_note_recalled_disabled_noop")


def test_note_recalled_enabled_records():
    """reinforce 开启 → note_recalled 返回条数，recall_count 递增。"""
    b = MemoryBackend()
    b._reinforce_enabled = True
    b._reinforce_bonus = 0.1
    assert b.note_recalled(["m1", "m2", "m1"]) == 3
    counts = b._recall_counts_dict()
    assert counts["m1"] == 2 and counts["m2"] == 1
    # 空/None 列表 → 0，不崩
    assert b.note_recalled(None) == 0
    assert b.note_recalled([None, ""]) == 0
    print("  OK test_note_recalled_enabled_records")


def test_note_recalled_zero_bonus_noop():
    """bonus<=0（默认 0）→ 即使 enabled 也不记录（恒等无副作用）。"""
    b = MemoryBackend()
    b._reinforce_enabled = True
    b._reinforce_bonus = 0.0
    assert b.note_recalled(["m1"]) == 0
    assert b._recall_counts_dict() == {}
    print("  OK test_note_recalled_zero_bonus_noop")


# ── _effective_importance 加成与封顶 ─────────────────────

def test_effective_importance_identity_when_disabled():
    """reinforce 关闭 → _effective_importance 恒等返回 raw importance。"""
    b = MemoryBackend()
    mem = {"id": "m1", "importance": 0.7}
    assert b._effective_importance(mem) == 0.7
    print("  OK test_effective_importance_identity_when_disabled")


def test_effective_importance_boosted_by_recall():
    """reinforce 开启：recall_count=3, bonus=0.1 → 0.7×(1+0.3)=0.91。"""
    b = MemoryBackend()
    b._reinforce_enabled = True
    b._reinforce_bonus = 0.1
    b.note_recalled(["m1", "m1", "m1"])
    assert abs(b._effective_importance({"id": "m1", "importance": 0.7}) - 0.91) < 1e-9
    # 未召回记忆 → 不加成
    assert b._effective_importance({"id": "m2", "importance": 0.7}) == 0.7
    print("  OK test_effective_importance_boosted_by_recall")


def test_effective_importance_caps_at_one():
    """加成封顶 1.0：高 importance + 高 count 不越界。"""
    b = MemoryBackend()
    b._reinforce_enabled = True
    b._reinforce_bonus = 0.5
    b.note_recalled(["m1"] * 10)
    assert b._effective_importance({"id": "m1", "importance": 0.9}) == 1.0
    print("  OK test_effective_importance_caps_at_one")


# ── Mem0Backend._persist_recall 写回 ─────────────────────

def test_persist_recall_writes_recall_count():
    """Mem0Backend._persist_recall → FakeMem0.update_memory(recall_count)。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1")], Path(td),
                     reinforce_enabled=True, reinforce_bonus=0.1)
        b._persist_recall("m1", 3)
        assert b._m.updated == [("m1", {"metadata": {"recall_count": 3}})]
    print("  OK test_persist_recall_writes_recall_count")


def test_note_recalled_persists_via_backend():
    """开启后 note_recalled → 计数 + 经 _persist_recall 写回 update_memory。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1"), _row("另一条", mem_id="m2")],
                     Path(td), reinforce_enabled=True, reinforce_bonus=0.1)
        n = b.note_recalled(["m1"])
        assert n == 1
        updated = {mid: data for mid, data in b._m.updated}
        assert updated["m1"]["metadata"]["recall_count"] == 1
    print("  OK test_note_recalled_persists_via_backend")


# ── 召回即强化（read 侧接线） ────────────────────────────

def test_search_with_forgetting_records_recall():
    """search_with_forgetting 返回结果 → 每条 id 记一次 recall。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1"), _row("另一条", mem_id="m2")],
                     Path(td), reinforce_enabled=True, reinforce_bonus=0.1)
        out = b.search_with_forgetting("咖啡", limit=5)
        assert len(out) == 2
        counts = b._recall_counts_dict()
        assert counts.get("m1") == 1 and counts.get("m2") == 1
    print("  OK test_search_with_forgetting_records_recall")


def test_search_with_forgetting_noop_when_disabled():
    """reinforce 默认关闭 → search_with_forgetting 不产生 recall 副作用。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1")], Path(td))
        b.search_with_forgetting("咖啡", limit=5)
        assert b._recall_counts_dict() == {}
        assert b._m.updated == []
    print("  OK test_search_with_forgetting_noop_when_disabled")


def test_random_memory_with_forgetting_records_recall():
    """random_memory_with_forgetting 选中一条 → 该条 id 记一次 recall。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1")], Path(td),
                     reinforce_enabled=True, reinforce_bonus=0.1)
        import random as _rnd
        _rnd.seed(42)
        m = b.random_memory_with_forgetting(min_importance=0.1)
        assert m is not None and m["id"] == "m1"
        assert b._recall_counts_dict().get("m1") == 1
    print("  OK test_random_memory_with_forgetting_records_recall")


def test_apply_forgetting_score_boost():
    """_apply_forgetting 中：被召回记忆 _score 高于未召回（同 importance/年龄）。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1"), _row("同文本的另一条", mem_id="m2")],
                     Path(td), reinforce_enabled=True, reinforce_bonus=0.2)
        now = datetime.now(CST)
        rows = [
            {"id": "recalled", "text": "一起看过的电影", "importance": 0.7,
             "timestamp": (now - timedelta(hours=2)).timestamp()},
            {"id": "fresh", "text": "一起看过的电影，很好看", "importance": 0.7,
             "timestamp": (now - timedelta(hours=2)).timestamp()},
        ]
        b.note_recalled(["recalled"] * 5)  # 5 次召回 → ×(1+0.2×5)=×2（封顶 1.0）
        scored = b._apply_forgetting([dict(r) for r in rows], now)
        # _apply_forgetting 末尾 pop 掉 _score，返回按 _score 降序排列
        assert scored[0]["id"] == "recalled", \
            f"被召回记忆应排前, got {[m['id'] for m in scored]}"
    print("  OK test_apply_forgetting_score_boost")


if __name__ == "__main__":
    print("test_memory_reinforce.py\n")
    tests = [
        test_note_recalled_disabled_noop,
        test_note_recalled_enabled_records,
        test_note_recalled_zero_bonus_noop,
        test_effective_importance_identity_when_disabled,
        test_effective_importance_boosted_by_recall,
        test_effective_importance_caps_at_one,
        test_persist_recall_writes_recall_count,
        test_note_recalled_persists_via_backend,
        test_search_with_forgetting_records_recall,
        test_search_with_forgetting_noop_when_disabled,
        test_random_memory_with_forgetting_records_recall,
        test_apply_forgetting_score_boost,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1
    print(f"\n{'=' * 40}")
    total = len(tests)
    print(f"ALL {total} reinforce tests, {total - failed} passed, {failed} failed.")
    sys.exit(1 if failed else 0)
