#!/usr/bin/env python3
"""test_emotion_tagging.py — B2 情绪-记忆耦合（写侧打标 + 读侧加权）单元测试

覆盖: emotion_tag_snapshot 离散档 / daemon _mem0_autowrite 写侧打标
（emotion_tagging=True → metadata.emotion_tag + user_mood；False 恒等无标）/
mem0 _row 契约透传 emotion_tag / base 读侧相似度加权（emotion_tag_weight>0
相近记忆排前）/ 相似度函数边界（None/非 dict/空 dict → 0）。
"""

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import re
import tempfile
from pathlib import Path

from datetime import datetime, timezone, timedelta
CST = timezone(timedelta(hours=8))

from memory import Mem0Backend
from memory.base import MemoryBackend
from chiguo_state import emotion_tag_snapshot
from chiguo_daemon import DecisionEngine


class FakeMem0:
    """mem0 Memory 最小模拟：记录 add 调用，search 返回注入结果。"""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.add_calls = []

    def search(self, query, filters=None, top_k=10):
        return {"results": list(self._results)}

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self._results)}

    def add(self, messages, user_id=None, metadata=None):
        self.add_calls.append({"messages": messages, "user_id": user_id, "metadata": metadata})
        return {"results": []}


def _daemon(tmp: str) -> DecisionEngine:
    """构造隔离 daemon（临时 toml/日志/记忆路径，禁用真实 mem0）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(tmp) / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{Path(tmp) / "no_history.db"}"', src)
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return DecisionEngine(str(cfg_path), str(Path(tmp) / "decisions.jsonl"))


def _fake_backend(results, tmp: Path = None) -> Mem0Backend:
    b = Mem0Backend(qdrant_path=str(Path(tmp) / "qdrant") if tmp else "/tmp/qdrant-tag",
                    history_db=str(Path(tmp) / "h.db") if tmp else "/tmp/h-tag.db")
    b._available = True
    b._last_probe = time.time() + 3600
    b._m = FakeMem0(results)
    return b


def test_emotion_tag_snapshot_levels():
    """离散档：≤30 low / ≥70 high / 中段 mid。"""
    class _E:
        loneliness = 20.0
        affection = 50.0
        anxiety = 80.0
        energy = 55.0
    tag = emotion_tag_snapshot(_E())
    assert tag == {"loneliness": "low", "affection": "mid", "anxiety": "high", "energy": "mid"}, tag
    print("  OK test_emotion_tag_snapshot_levels")


def test_write_side_enabled_tags_metadata():
    """emotion_tagging=True → _mem0_autowrite 在 metadata 写入 emotion_tag（含 user_mood）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _daemon(td)
        eng.state.memory_bridge = _fake_backend([], Path(td))
        eng.state.config["memory"] = dict(eng.state.config.get("memory", {}))
        eng.state.config["memory"]["emotion_tagging"] = True
        # 注入 user_mood 感知
        eng.state.cooldown.user_mood = {"mood": "low", "intensity": 0.7, "at": "2026-08-08T10:00:00+08:00"}
        eng._mem0_autowrite("哥哥今天在学校被老师表扬了，好开心呀")
        calls = eng.state.memory_bridge._m.add_calls
        assert len(calls) == 1, "应写入一次"
        meta = calls[0]["metadata"]
        assert isinstance(meta.get("emotion_tag"), dict), f"应含 emotion_tag: {meta}"
        tag = meta["emotion_tag"]
        assert tag.get("user_mood") == "low", f"应含 user_mood: {tag}"
        for dim in ("loneliness", "affection", "anxiety", "energy"):
            assert tag.get(dim) in ("low", "mid", "high"), f"{dim} 档位非法: {tag}"
        assert meta["category"] == "conversation"  # 既有字段保留
    print("  OK test_write_side_enabled_tags_metadata")


def test_write_side_off_identity():
    """emotion_tagging=False（默认）→ metadata 无 emotion_tag（恒等）。"""
    with tempfile.TemporaryDirectory() as td:
        eng = _daemon(td)
        eng.state.memory_bridge = _fake_backend([], Path(td))
        eng._mem0_autowrite("哥哥今天在学校被老师表扬了，好开心呀")
        calls = eng.state.memory_bridge._m.add_calls
        assert len(calls) == 1
        assert "emotion_tag" not in calls[0]["metadata"], calls[0]["metadata"]
    print("  OK test_write_side_off_identity")


def test_row_contract_passes_emotion_tag():
    """_row 契约透传 emotion_tag（dict 保留；非 dict → None）。"""
    b = _fake_backend([], Path("/tmp"))
    row = b._row({"id": "m1", "memory": "x", "metadata": {"emotion_tag": {"loneliness": "high"}},
                  "created_at": "2026-08-08T10:00:00+00:00"})
    assert row["emotion_tag"] == {"loneliness": "high"}
    bad = b._row({"id": "m2", "memory": "y", "metadata": {"emotion_tag": "oops"},
                  "created_at": "2026-08-08T10:00:00+00:00"})
    assert bad["emotion_tag"] is None
    print("  OK test_row_contract_passes_emotion_tag")


def test_read_side_weighting_boosts_similar():
    """读侧：emotion_tag_weight>0 时情绪相近记忆排前（weight=0 恒等）。"""
    now = datetime(2026, 8, 8, 12, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        def _row(mem_id, tag):
            m = {"id": mem_id, "memory": f"记忆{mem_id}",
                 "metadata": {"importance": 0.9},
                 "created_at": "2026-08-08T10:00:00+00:00"}
            if tag:
                m["metadata"]["emotion_tag"] = tag
            return m
        rows = [_row("a", None), _row("b", {"loneliness": "high", "anxiety": "mid"})]
        req = {"loneliness": "high", "anxiety": "mid"}
        b0 = _fake_backend(rows, Path(td))
        ids0 = [r["id"] for r in b0.search_with_forgetting("记忆", limit=5,
                                                           emotion_tag=req, emotion_tag_weight=0.0)]
        assert ids0[0] == "a", f"weight=0 应恒等（原始顺序）: {ids0}"
        b1 = _fake_backend(rows, Path(td))
        ids1 = [r["id"] for r in b1.search_with_forgetting("记忆", limit=5,
                                                           emotion_tag=req, emotion_tag_weight=3.0)]
        assert ids1[0] == "b", f"weight=3 应把情绪相近记忆排前: {ids1}"
    print("  OK test_read_side_weighting_boosts_similar")


def test_similarity_edge_cases():
    """相似度边界：None/非 dict/空请求 → 0；全匹配 → 1。"""
    assert MemoryBackend.emotion_tag_similarity(None, {"loneliness": "high"}) == 0.0
    assert MemoryBackend.emotion_tag_similarity("notadict", {"loneliness": "high"}) == 0.0
    assert MemoryBackend.emotion_tag_similarity({"loneliness": "high"}, {}) == 0.0
    assert MemoryBackend.emotion_tag_similarity({"loneliness": "high"}, {"loneliness": "high"}) == 1.0
    assert MemoryBackend.emotion_tag_similarity({"loneliness": "high"}, {"loneliness": "low"}) == 0.0
    assert MemoryBackend.emotion_tag_similarity(
        {"loneliness": "high", "anxiety": "mid"}, {"loneliness": "high", "anxiety": "low"}) == 0.5
    print("  OK test_similarity_edge_cases")


def test_topic_picker_read_side_wired():
    """B2 修复：emotion_tagging=True 时 TopicPicker._memory_topic 实际把当前情绪
    emotion_tag + emotion_tag_weight 传进记忆检索（此前读侧为死代码，无人传递）；
    关闭/weight<=0 → 传 None/0.0（恒等）。"""
    from chiguo_topics import TopicPicker
    import random as _random

    class FakeBridge:
        available = True

        def __init__(self):
            self.calls = []

        def search_with_forgetting(self, query, limit=10, min_importance=0.3,
                                   emotion_tag=None, emotion_tag_weight=0.0, **kw):
            self.calls.append(("search", emotion_tag, emotion_tag_weight))
            return []

        def random_memory_with_forgetting(self, min_importance=0.5,
                                          prefer_categories=None,
                                          emotion_tag=None, emotion_tag_weight=0.0, **kw):
            self.calls.append(("random", emotion_tag, emotion_tag_weight))
            return None

    class _E:
        loneliness = 85.0
        affection = 50.0
        anxiety = 30.0
        energy = 55.0

    class FakeState:
        def __init__(self, tagging=True, weight=2.0):
            self.config = {"memory": {"emotion_tagging": tagging,
                                      "emotion_tag_weight": weight}}
            self.memory_bridge = FakeBridge()
            self.cooldown = type("_Cd", (), {"trigger_history": []})()
            self.cooldown.get_trigger_history = lambda: self.cooldown.trigger_history
            self.emotion = _E()

    fs = FakeState()
    tp = TopicPicker(fs, {})
    _random.seed(1)  # random.random()≈0.13 < 0.5 → 走 search 分支
    tp._memory_topic()
    assert fs.memory_bridge.calls, "应发起记忆检索"
    kind, tag, w = fs.memory_bridge.calls[0]
    assert kind == "search"
    assert w == 2.0, f"应传 emotion_tag_weight: {w}"
    assert isinstance(tag, dict) and tag.get("loneliness") == "high", f"应传 emotion_tag: {tag}"

    # 关闭（默认恒等）→ 不传 emotion_tag/weight
    fs_off = FakeState(tagging=False)
    tp_off = TopicPicker(fs_off, {})
    _random.seed(1)
    tp_off._memory_topic()
    kind2, tag2, w2 = fs_off.memory_bridge.calls[0]
    assert tag2 is None and w2 == 0.0, f"关闭应恒等: {tag2}/{w2}"
    print("  OK test_topic_picker_read_side_wired")


if __name__ == "__main__":
    print("test_emotion_tagging.py\n")
    tests = [
        test_emotion_tag_snapshot_levels,
        test_write_side_enabled_tags_metadata,
        test_write_side_off_identity,
        test_row_contract_passes_emotion_tag,
        test_read_side_weighting_boosts_similar,
        test_similarity_edge_cases,
        test_topic_picker_read_side_wired,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} emotion-tagging tests passed.")
