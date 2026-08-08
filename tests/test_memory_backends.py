#!/usr/bin/env python3
"""test_memory_backends.py — 记忆后端抽象测试（v1.9 默认后端 mem0）

覆盖: Mem0Backend 行契约映射/查询/加权随机/统计/降级（FakeMem0 模拟，
不依赖真实 ollama/LLM/网络）、factory 的 mem0/自定义类路径分流、
Ebbinghaus 包装在基类对所有后端生效。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import Mem0Backend, create_backend  # noqa: E402


class FakeMem0:
    """mem0 Memory 最小模拟：search/get_all/add。"""

    def __init__(self, results):
        self._results = results
        self.add_calls = []

    def search(self, query, filters=None, top_k=10):
        # 模拟 mem0 原生 metadata 键过滤（category 等）
        cat = (filters or {}).get("category")
        out = [r for r in self._results if not cat or (r.get("metadata") or {}).get("category") == cat]
        return {"results": out}

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self._results)}

    def add(self, messages, user_id=None, metadata=None):
        self.add_calls.append({"messages": messages, "user_id": user_id, "metadata": metadata})
        return {"results": []}


def _mem0_row(text="哥哥喜欢喝美式咖啡", category="preferences", importance=0.9,
              created="2026-08-08T10:00:00+00:00", mem_id="m1", **meta_extra):
    meta = {"category": category, "importance": importance}
    meta.update(meta_extra)
    return {"id": mem_id, "memory": text, "hash": "h", "metadata": meta,
            "score": 0.6, "created_at": created, "updated_at": created,
            "user_id": "chiguo"}


def _fake_backend(results, tmp: Path = None) -> Mem0Backend:
    """Mem0Backend 注入 FakeMem0，跳过真实连接探测。"""
    b = Mem0Backend(qdrant_path=str(Path(tmp) / "qdrant") if tmp else "/tmp/qdrant-test",
                    history_db=str(Path(tmp) / "h.db") if tmp else "/tmp/h-test.db")
    b._available = True
    b._m = FakeMem0(results)
    return b


# ── 行契约 ────────────────────────────────────────────────

def test_row_contract():
    """mem0 result → 统一行契约 dict（消费方依赖的字段全在）。"""
    b = Mem0Backend()
    r = _mem0_row(scope="global", memory_category="preferences",
                  l0_abstract="咖啡", tier="long_term", source="daemon")
    row = b._row(r)
    for key in ("id", "text", "category", "scope", "importance", "timestamp",
                "datetime", "memory_category", "l0_abstract", "l2_content",
                "tier", "source"):
        assert key in row, f"缺字段 {key}"
    assert row["text"] == "哥哥喜欢喝美式咖啡"
    assert row["category"] == "preferences"
    assert row["importance"] == 0.9
    assert row["source"] == "daemon"
    assert row["timestamp"] > 0, "ISO 时间应解析为 epoch"
    print("  OK test_row_contract")


def test_row_defaults():
    """字段缺失兜底：importance 缺省 0.5（非 0），metadata None 不炸。"""
    b = Mem0Backend()
    r = {"id": "m2", "memory": "随便一句", "metadata": None, "created_at": ""}
    row = b._row(r)
    assert row["importance"] == 0.5
    assert row["category"] == "" and row["memory_category"] == "?"
    assert row["timestamp"] == 0.0 and row["tier"] == "working"
    print("  OK test_row_defaults")


# ── 查询 ──────────────────────────────────────────────────

def test_search_basic():
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([_mem0_row(), _mem0_row("迟菓的生日是 5 月 11 日", "events", 0.8, mem_id="m2")], Path(td))
        r = b.search("咖啡", limit=5)
        assert len(r) == 2 and r[0]["text"].startswith("哥哥")
        # category 过滤（mem0 filters 原生支持 metadata 键）
        r2 = b.search("咖啡", limit=5, category="events")
        assert len(r2) == 1 and r2[0]["category"] == "events"
        # importance 过滤（0.2 条目被滤掉，0.9 保留）
        low = _mem0_row("随口一提", "chitchat", 0.2, mem_id="m3")
        b2 = _fake_backend([_mem0_row(), low], Path(td))
        r3 = b2.search("随口", limit=5, min_importance=0.5)
        assert len(r3) == 1 and all(x["importance"] >= 0.5 for x in r3)
    print("  OK test_search_basic")


def test_search_unavailable():
    """不可用（无 LLM key 来源）→ 空列表不抛。"""
    import memory.mem0_backend as mb
    orig = mb._pi_api_key
    mb._pi_api_key = lambda: None
    try:
        b = Mem0Backend(llm_api_key=None)
        assert not b.available
        assert b.search("x") == []
        assert b.random_memory() is None
        s = b.stats()
        assert not s["available"] and s["total_memories"] == 0
    finally:
        mb._pi_api_key = orig
    print("  OK test_search_unavailable")


def test_random_memory_weighted():
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([_mem0_row(importance=0.9), _mem0_row(importance=0.1, mem_id="m2")], Path(td))
        # 高 importance 条目应被稳定选中（权重 0.81 vs 0.01）
        picked = [b.random_memory(min_importance=0.3)["id"] for _ in range(30)]
        assert all(i == "m1" for i in picked), "加权随机应偏向高 importance"
        # prefer_categories 排序不炸
        m = b.random_memory(prefer_categories=["preferences"])
        assert m is not None
    print("  OK test_random_memory_weighted")


def test_stats():
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([_mem0_row(), _mem0_row("第二条", "events", 0.7, mem_id="m2")], Path(td))
        s = b.stats()
        assert s["available"] and s["total_memories"] == 2
        assert s["backend"] == "mem0" and s["db_path"]
    print("  OK test_stats")


# ── 写入 ──────────────────────────────────────────────────

def test_add_messages():
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([], Path(td))
        ok = b.add_messages([{"role": "user", "content": "我喜欢喝咖啡"},
                             {"role": "assistant", "content": "记住了"}],
                            metadata={"category": "preferences"})
        assert ok
        call = b._m.add_calls[-1]
        assert call["user_id"] == "chiguo"
        assert call["metadata"]["category"] == "preferences"
        # 不可用（无 key 来源）→ False 不抛
        import memory.mem0_backend as mb
        orig = mb._pi_api_key
        mb._pi_api_key = lambda: None
        try:
            b2 = Mem0Backend(llm_api_key=None)
            assert not b2.add_messages("文本")
        finally:
            mb._pi_api_key = orig
    print("  OK test_add_messages")


# ── Ebbinghaus 继承 ───────────────────────────────────────

def test_ebbinghaus_inherited():
    """Ebbinghaus 包装在基类，mem0 后端同样生效。"""
    import time
    now_ts = int(time.time())
    with tempfile.TemporaryDirectory() as td:
        b = _fake_backend([
            {"id": "old", "memory": "一起看过的电影", "metadata": {"importance": 0.9},
             "created_at": "2026-08-08T10:00:00+00:00"},
            {"id": "new", "memory": "主人新分享的记忆", "metadata": {"importance": 0.9},
             "created_at": "2026-08-09T10:00:00+00:00"},
        ], Path(td))
        r = b.search_with_forgetting("记忆", limit=5)
        assert r and r[0]["id"] == "new", "遗忘权重应让新记忆排前"
        w = b.ebbinghaus_weight({"timestamp": now_ts - 168 * 3600, "importance": 1.0}, strength=168.0)
        assert 0 < w < 1
    print("  OK test_ebbinghaus_inherited")


# ── factory ───────────────────────────────────────────────

def test_factory_mem0():
    with tempfile.TemporaryDirectory() as td:
        b = create_backend({"backend": "mem0"})
        assert isinstance(b, Mem0Backend)
        assert b.user_id == "chiguo"
        # 相对路径锚定 base_dir
        b2 = create_backend({"backend": "mem0", "mem0_qdrant_path": "qd", "mem0_history_db": "h.db"}, base_dir=td)
        assert b2.qdrant_path.startswith(td) and b2.history_db.startswith(td)
        # 默认（不写 backend）→ mem0
        b3 = create_backend({})
        assert isinstance(b3, Mem0Backend)
    print("  OK test_factory_mem0")


def test_factory_custom_class():
    """自定义类路径：importlib 动态加载；**kwargs 类全传。"""
    with tempfile.TemporaryDirectory() as td:
        b = create_backend({"backend": "memory.mem0_backend.Mem0Backend",
                            "user_id": "custom-user"}, base_dir=td)
        assert isinstance(b, Mem0Backend) and b.user_id == "custom-user"
    print("  OK test_factory_custom_class")


def test_factory_custom_class_bad():
    """自定义类不存在/非 MemoryBackend 子类 → 抛错（配置错误要暴露）。"""
    try:
        create_backend({"backend": "memory.mem0_backend.NoSuchClass"})
        raise AssertionError("应抛 AttributeError")
    except AttributeError:
        pass
    try:
        create_backend({"backend": "memory.factory.create_backend"})  # 函数不是类
        raise AssertionError("应抛 TypeError")
    except TypeError:
        pass
    print("  OK test_factory_custom_class_bad")


def test_factory_unknown_string():
    """未知非点分值 → 回退默认 mem0（不抛）。"""
    b = create_backend({"backend": "weird"})
    assert isinstance(b, Mem0Backend)
    print("  OK test_factory_unknown_string")


if __name__ == "__main__":
    test_row_contract()
    test_row_defaults()
    test_search_basic()
    test_search_unavailable()
    test_random_memory_weighted()
    test_stats()
    test_add_messages()
    test_ebbinghaus_inherited()
    test_factory_mem0()
    test_factory_custom_class()
    test_factory_custom_class_bad()
    test_factory_unknown_string()
    print(f"test_memory_backends.py: ALL 12 TESTS PASSED")
