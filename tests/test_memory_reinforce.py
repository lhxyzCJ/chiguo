#!/usr/bin/env python3
"""test_memory_reinforce.py — C2 Ebbinghaus 复习强化测试

覆盖: note_recalled 计数/默认关闭恒等、_effective_importance 加成与封顶、
_persist_recall 经 FakeMem0 update 写回 recall_count、
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

CST = timezone(timedelta(hours=8))

from memory import Mem0Backend  # noqa: E402
from memory.base import MemoryBackend  # noqa: E402


class FakeMem0:
    """mem0 Memory 最小模拟 + update/get 记录 + metadata 持久化（C2 写回与跨进程累积断言）。

    update 签名对齐真实 mem0 2.x：update(memory_id, text=None, metadata=None)。
    get(memory_id) 返回当前（含已 merge 的 metadata）记录，模拟真实 mem0 读回；
    _meta 模拟 mem0 持久化存储（update 的 metadata 为 merge 语义写回 _meta）。
    """

    def __init__(self, results):
        self._results = list(results)
        self.updated: list[tuple] = []
        self._meta = {r.get("id"): dict(r.get("metadata") or {}) for r in self._results}

    def search(self, query, filters=None, top_k=10):
        return {"results": list(self._results)}

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self._results)}

    def add(self, messages, user_id=None, metadata=None):
        return {"results": []}

    def get(self, memory_id):
        """模拟 mem0 Memory.get(memory_id)：返回当前（含已 merge 的 metadata）记录。"""
        for r in self._results:
            if r.get("id") == memory_id:
                return {**r, "metadata": dict(self._meta.get(memory_id, {}))}
        return None

    def update(self, memory_id, text=None, metadata=None):
        self.updated.append((memory_id, metadata or {}))
        # 模拟 mem0 update 的 metadata merge 语义：写回持久化存储（跨进程累积数据源）
        if memory_id in self._meta:
            self._meta[memory_id].update(metadata or {})
        else:
            self._meta[memory_id] = dict(metadata or {})


def _row(text="哥哥喜欢喝美式咖啡", importance=0.9, mem_id="m1",
         age_hours=1.0):
    ts = datetime.now(CST).timestamp() - age_hours * 3600
    return {"id": mem_id, "memory": text, "hash": "h",
            "metadata": {"category": "preferences", "importance": importance},
            "created_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "updated_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "user_id": "chiguo"}


def _backend(results, tmp: Path, fake: FakeMem0 = None, **kw) -> Mem0Backend:
    b = Mem0Backend(qdrant_path=str(tmp / "qdrant"), history_db=str(tmp / "h.db"),
                    **kw)
    b._available = True
    b._last_probe = time.time() + 3600  # 缓存恒命中，永不真实探测
    b._m = fake if fake is not None else FakeMem0(results)
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


def test_effective_importance_reads_row_recall_count():
    """#2 跨进程回读：cron 每 15 分钟起新进程，_recall_counts 从空开始——
    行 dict 里持久化的 recall_count 应参与加成（0.7×(1+0.1×3)=0.91）。"""
    b = MemoryBackend()
    b._reinforce_enabled = True
    b._reinforce_bonus = 0.1
    # 无内存侧计数（新进程），仅行内 recall_count
    assert abs(b._effective_importance({"id": "m1", "importance": 0.7,
                                        "recall_count": 3}) - 0.91) < 1e-9
    # 行内无 recall_count → 恒等
    assert b._effective_importance({"id": "m2", "importance": 0.7}) == 0.7
    # 非法 recall_count（字符串垃圾）→ 兜底 0
    assert b._effective_importance({"id": "m3", "importance": 0.7,
                                    "recall_count": "nan"}) == 0.7
    print("  OK test_effective_importance_reads_row_recall_count")


def test_row_maps_recall_count_from_metadata():
    """Mem0Backend._row 把 mem0 metadata.recall_count 映射进行 dict（#2 回读数据源）。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([], Path(td))
        raw = {"id": "m1", "memory": "哥哥喜欢喝美式咖啡",
               "metadata": {"category": "preferences", "importance": 0.9,
                            "recall_count": 5},
               "created_at": datetime.now(timezone.utc).isoformat(),
               "user_id": "chiguo"}
        row = b._row(raw)
        assert row["recall_count"] == 5, row
        # 无 recall_count / 非法 → 0
        assert b._row({**raw, "metadata": {"category": "c"}})["recall_count"] == 0
        assert b._row({**raw, "metadata": {"recall_count": "x"}})["recall_count"] == 0
    print("  OK test_row_maps_recall_count_from_metadata")


# ── Mem0Backend._persist_recall 写回 ─────────────────────

def test_persist_recall_writes_recall_count():
    """Mem0Backend._persist_recall → FakeMem0.update(metadata recall_count)。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1")], Path(td),
                     reinforce_enabled=True, reinforce_bonus=0.1)
        b._persist_recall("m1", 3)
        assert b._m.updated == [("m1", {"recall_count": 3})]
    print("  OK test_persist_recall_writes_recall_count")


def test_note_recalled_persists_via_backend():
    """开启后 note_recalled → 计数 + 经 _persist_recall 写回 update。"""
    with tempfile.TemporaryDirectory() as td:
        b = _backend([_row(mem_id="m1"), _row("另一条", mem_id="m2")],
                     Path(td), reinforce_enabled=True, reinforce_bonus=0.1)
        n = b.note_recalled(["m1"])
        assert n == 1
        updated = {mid: data for mid, data in b._m.updated}
        assert updated["m1"]["recall_count"] == 1
    print("  OK test_note_recalled_persists_via_backend")


# ── A2 跨进程累积（Issue #133）──────────────────────────

def test_recall_count_accumulates_across_processes():
    """A2 跨进程累积：同一 mem0 后端（共享 FakeMem0 持久化）先后由两个独立
    MemoryBackend 实例各 note_recalled 同 memory_id → 第二次持久化 recall_count === 2。

    当前 bug：cron 每 15 分钟新进程，_recall_counts dict 从空开始，
    counts.get(mid, 0)+1 → 第二次把持久化旧值覆盖成 1，加权永不累积。"""
    with tempfile.TemporaryDirectory() as td:
        fake = FakeMem0([_row(mem_id="m1")])
        b1 = _backend([_row(mem_id="m1")], Path(td), fake=fake,
                      reinforce_enabled=True, reinforce_bonus=0.1)
        b2 = _backend([_row(mem_id="m1")], Path(td), fake=fake,
                      reinforce_enabled=True, reinforce_bonus=0.1)
        # 进程 1：新实例 dict 空 → 读持久化 0 → 写 1
        assert b1.note_recalled(["m1"]) == 1
        assert fake.get("m1")["metadata"]["recall_count"] == 1
        # 进程 2：新实例 dict 空 → 必须读回持久化 1 再 +1（当前 bug：覆盖写 1）
        assert b2.note_recalled(["m1"]) == 1
        assert fake.get("m1")["metadata"]["recall_count"] == 2
    print("  OK test_recall_count_accumulates_across_processes")


def test_mock_backend_cross_process_accumulates():
    """A2 mock 后端覆盖：MemoryBackend 子类模拟共享持久化存储，
    两个独立实例各 note_recalled 同 memory_id → 第二次持久化 recall_count === 2。"""
    store: dict[str, int] = {}

    class _SharedPersistBackend(MemoryBackend):
        def _persist_recall(self, memory_id, count):
            store[memory_id] = count

        def _load_recall_count(self, memory_id):
            return store.get(memory_id, 0)

    b1 = _SharedPersistBackend()
    b2 = _SharedPersistBackend()
    for b in (b1, b2):
        b._reinforce_enabled = True
        b._reinforce_bonus = 0.1
    assert b1.note_recalled(["m1"]) == 1
    assert store["m1"] == 1
    # 新实例 dict 空：读持久化 1 → 写 2（当前 bug：覆盖写 1）
    assert b2.note_recalled(["m1"]) == 1
    assert store["m1"] == 2
    print("  OK test_mock_backend_cross_process_accumulates")


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
