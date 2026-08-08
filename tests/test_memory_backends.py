#!/usr/bin/env python3
"""test_memory_backends.py — 记忆后端抽象测试（v1.8 解耦）

覆盖: JsonMemoryBackend 查询/加权随机/统计/降级、
factory 的 auto/lancedb/json/自定义类路径分流、
Ebbinghaus 包装在基类对所有后端生效。
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory import JsonMemoryBackend, LanceDbBackend, create_backend  # noqa: E402

MEMORIES = [
    {"text": "哥哥喜欢喝美式咖啡", "category": "preferences", "importance": 0.9, "timestamp": 0},
    {"text": "迟菓的生日是 5 月 11 日", "category": "events", "importance": 0.8},
    {"text": "Chiguo 周报提醒", "importance": 0.5},
    {"text": "随口一提的日常", "importance": 0.2},
    {"text": "带 importance 字符串的条目", "importance": "0.6"},
]


def _write_mem(tmp: Path, items: list = None) -> Path:
    p = tmp / "mem.json"
    p.write_text(json.dumps(items if items is not None else MEMORIES, ensure_ascii=False))
    return p


def test_json_available():
    with tempfile.TemporaryDirectory() as td:
        p = _write_mem(Path(td))
        b = JsonMemoryBackend(path=str(p))
        assert b.available
        b2 = JsonMemoryBackend(path=str(Path(td) / "nope.json"))
        assert not b2.available
    print("  OK test_json_available")


def test_json_search():
    with tempfile.TemporaryDirectory() as td:
        b = JsonMemoryBackend(path=str(_write_mem(Path(td))))
        r = b.search("咖啡", limit=5)
        assert len(r) == 1 and r[0]["text"].startswith("哥哥喜欢")
        assert r[0]["memory_category"] == "?"
        assert r[0]["source"] == "manual"
        # 大小写不敏感
        assert len(b.search("chiguo", limit=5)) == 1
        assert len(b.search("CHIGUO", limit=5)) == 1
        # category 过滤
        assert len(b.search("生日", limit=5, category="events")) == 1
        assert len(b.search("生日", limit=5, category="preferences")) == 0
        # importance 过滤（0.2 条目被滤掉）
        r2 = b.search("随口", limit=5, min_importance=0.5)
        assert len(r2) == 0
        # 无结果
        assert b.search("不存在的关键词xyz", limit=5) == []
    print("  OK test_json_search")


def test_json_search_importance_string():
    """字符串 importance 清洗为数值，不被 TypeError 打断。"""
    with tempfile.TemporaryDirectory() as td:
        b = JsonMemoryBackend(path=str(_write_mem(Path(td))))
        r = b.search("importance", limit=5, min_importance=0.5)
        assert len(r) == 1 and r[0]["importance"] == 0.6
    print("  OK test_json_search_importance_string")


def test_json_random_and_stats():
    with tempfile.TemporaryDirectory() as td:
        b = JsonMemoryBackend(path=str(_write_mem(Path(td))))
        m = b.random_memory(min_importance=0.5)
        assert m is not None and m["importance"] >= 0.5
        s = b.stats()
        assert s["total_memories"] == 5 and s["available"]
        # 不可用后端 stats
        b2 = JsonMemoryBackend(path=str(Path(td) / "nope.json"))
        s2 = b2.stats()
        assert not s2["available"] and s2["total_memories"] == 0
    print("  OK test_json_random_and_stats")


def test_json_ebbinghaus_inherited():
    """Ebbinghaus 包装在基类，JSON 后端同样生效。"""
    import time
    now_ts = int(time.time())
    with tempfile.TemporaryDirectory() as td:
        p = _write_mem(Path(td), [
            {"text": "迟菓的旧记忆：一起看过的电影", "importance": 0.9, "timestamp": now_ts - 30 * 3600},
            {"text": "主人新分享的记忆：喜欢的歌", "importance": 0.9, "timestamp": now_ts},
        ])
        b = JsonMemoryBackend(path=str(p))
        r = b.search_with_forgetting("记忆", limit=5)
        assert r and r[0]["text"] == "主人新分享的记忆：喜欢的歌", "遗忘权重应让新记忆排前"
        m = b.random_memory_with_forgetting(min_importance=0.5)
        assert m is not None
        w = b.ebbinghaus_weight({"timestamp": now_ts - 168 * 3600, "importance": 1.0}, strength=168.0)
        assert 0 < w < 1
    print("  OK test_json_ebbinghaus_inherited")


def test_factory_json():
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        p = _write_mem(td)
        b = create_backend({"backend": "json", "manual_path": str(p)})
        assert isinstance(b, JsonMemoryBackend) and b.available
        # 相对路径锚定 base_dir
        b2 = create_backend({"backend": "json", "manual_path": "mem.json"}, base_dir=td)
        assert isinstance(b2, JsonMemoryBackend) and b2.available
    print("  OK test_factory_json")


def test_factory_lancedb():
    """显式 lancedb：实例化成功（可用性惰性探测，缺库/缺路径不抛）。"""
    with tempfile.TemporaryDirectory() as td:
        b = create_backend({"backend": "lancedb", "lancedb_path": str(Path(td) / "no_db")})
        assert isinstance(b, LanceDbBackend)
        # 路径不存在 → available=False，查询空列表不抛
        assert not b.available
        assert b.search("x") == []
    print("  OK test_factory_lancedb")


def test_factory_auto_fallback_json():
    """auto：lancedb 不可导入（注入坏路径）→ JSON 兜底；可导入 → LanceDB。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        p = _write_mem(td)
        # 注入不可导入路径（lancedb 已装时用坏 sys.path 无法阻止——用 monkeypatch find_spec）
        import memory.factory as factory
        orig = factory._lancedb_importable
        factory._lancedb_importable = lambda: False
        try:
            b = create_backend({"manual_path": str(p)}, base_dir=td)
            assert isinstance(b, JsonMemoryBackend), "auto 无 lancedb → JSON 兜底"
        finally:
            factory._lancedb_importable = orig
        # 可导入 → LanceDB
        factory._lancedb_importable = lambda: True
        try:
            b2 = create_backend({"manual_path": str(p)}, base_dir=td)
            assert isinstance(b2, LanceDbBackend), "auto 有 lancedb → LanceDB"
        finally:
            factory._lancedb_importable = orig
    print("  OK test_factory_auto_fallback_json")


def test_factory_custom_class():
    """自定义类路径：importlib 动态加载；签名过滤 kwargs。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        p = _write_mem(td)
        # 内置类经点分路径接入（manual_path 不在 JsonMemoryBackend 签名 → 过滤）
        b = create_backend(
            {"backend": "memory.json.JsonMemoryBackend", "manual_path": str(p)},
            base_dir=td,
        )
        assert isinstance(b, JsonMemoryBackend) and b.available
    print("  OK test_factory_custom_class")


def test_factory_custom_class_bad():
    """自定义类不存在/非 MemoryBackend 子类 → 抛错（配置错误要暴露，不静默降级）。"""
    with tempfile.TemporaryDirectory() as td:
        try:
            create_backend({"backend": "memory.json.NoSuchClass"})
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
    """未知非点分值 → 回退 auto 语义（不抛）。"""
    with tempfile.TemporaryDirectory() as td:
        b = create_backend({"backend": "weird"}, base_dir=td)
        assert isinstance(b, (LanceDbBackend, JsonMemoryBackend))
    print("  OK test_factory_unknown_string")


def test_json_malformed_file():
    """损坏 JSON 文件 → available=False，查询不抛。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        p = td / "mem.json"
        p.write_text("{broken json")
        b = JsonMemoryBackend(path=str(p))
        assert not b.available
        assert b.search("x") == []
        assert b.random_memory() is None
    print("  OK test_json_malformed_file")


if __name__ == "__main__":
    test_json_available()
    test_json_search()
    test_json_search_importance_string()
    test_json_random_and_stats()
    test_json_ebbinghaus_inherited()
    test_factory_json()
    test_factory_lancedb()
    test_factory_auto_fallback_json()
    test_factory_custom_class()
    test_factory_custom_class_bad()
    test_factory_unknown_string()
    test_json_malformed_file()
    print(f"test_memory_backends.py: ALL {12} TESTS PASSED")
