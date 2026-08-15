#!/usr/bin/env python3
"""test_memory_consolidate.py — C1 确定性记忆巩固测试

覆盖: consolidate_plan 纯函数（去重降权/过期/保留）、Mem0Backend.consolidate
写回（FakeMem0 的 delete/update 记录）、dry_run、不可用降级、
daemon 空闲静默路径 _maybe_consolidate（FakeBridge 注入 + 门控断言）。
全部零 LLM、零网络（不触真实 ollama/qdrant）。
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

from memory import Mem0Backend
from memory.base import MemoryBackend  # noqa: E402


def _now() -> datetime:
    return datetime.now(CST)


class FakeMem0:
    """mem0 Memory 最小模拟 + delete/update 记录（C1 写回断言）。

    update 签名对齐真实 mem0 2.x：update(memory_id, text=None, metadata=None)。
    """

    def __init__(self, results):
        self._results = list(results)
        self.deleted: list[str] = []
        self.updated: list[tuple] = []

    def search(self, query, filters=None, top_k=10):
        return {"results": list(self._results)}

    def get_all(self, filters=None, top_k=100):
        return {"results": list(self._results)}

    def add(self, messages, user_id=None, metadata=None):
        return {"results": []}

    def delete(self, memory_id):
        self.deleted.append(memory_id)

    def update(self, memory_id, text=None, metadata=None):
        self.updated.append((memory_id, metadata))


def _row(text, importance=0.8, age_hours=1.0, mem_id=None, created=None):
    """构造 mem0 result 行（created_at 由 age_hours 相对 now 反推）。"""
    ts = _now().timestamp() - age_hours * 3600
    created = created or datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    return {"id": mem_id or f"m-{abs(hash(text)) % 100000}",
            "memory": text, "hash": "h",
            "metadata": {"category": "conversation", "importance": importance},
            "created_at": created, "updated_at": created, "user_id": "chiguo"}


def _fake_backend(results, tmp: Path) -> Mem0Backend:
    b = Mem0Backend(qdrant_path=str(tmp / "qdrant"), history_db=str(tmp / "h.db"))
    b._available = True
    b._last_probe = time.time() + 3600  # 缓存恒命中，永不真实探测
    b._m = FakeMem0(results)
    return b


# ── consolidate_plan 纯函数 ───────────────────────────────

def test_plan_duplicate_demote():
    """近似重复对：保留 importance 高的一条，另一条 importance 减半 + _consolidated。"""
    rows = [
        {"id": "a", "text": "哥哥喜欢喝美式咖啡，不加糖", "importance": 0.9, "timestamp": _now().timestamp()},
        {"id": "b", "text": "哥哥喜欢喝美式咖啡，不加糖", "importance": 0.5, "timestamp": _now().timestamp()},
    ]
    plan = MemoryBackend.consolidate_plan(rows, _now(), sim_threshold=0.85)
    demoted_ids = [r["id"] for r in plan["demoted"]]
    assert demoted_ids == ["b"], f"低重要度 b 应被降权, got {demoted_ids}"
    b = plan["demoted"][0]
    assert b["_consolidated"] is True and b["consolidated_with"] == "a"
    assert abs(b["importance"] - 0.25) < 1e-9, f"0.5 减半 = 0.25, got {b['importance']}"
    kept_ids = [r["id"] for r in plan["kept"]]
    assert "a" in kept_ids and "b" not in kept_ids
    print("  OK test_plan_duplicate_demote")


def test_plan_keeps_high_importance_when_equal_text():
    """相同文本：保留新/重要的一条（排序靠前者），不双删。"""
    rows = [
        {"id": "x", "text": "一起去过苏州旅行，住平江路", "importance": 0.6, "timestamp": _now().timestamp()},
        {"id": "y", "text": "一起去过苏州旅行，住平江路", "importance": 0.9, "timestamp": _now().timestamp()},
    ]
    plan = MemoryBackend.consolidate_plan(rows, _now(), sim_threshold=0.85)
    assert [r["id"] for r in plan["demoted"]] == ["x"], "低重要度 x 应被降权"
    assert "y" in [r["id"] for r in plan["kept"]]
    print("  OK test_plan_keeps_high_importance_when_equal_text")


def test_plan_expire_old_low_importance():
    """低重要度 + 超龄 → 标记 _expired（候选删除）。"""
    now = _now()
    rows = [
        {"id": "old", "text": "多年前的琐事", "importance": 0.1,
         "timestamp": (now - timedelta(days=40)).timestamp()},  # 960h > 720h
        {"id": "new", "text": "最近的新记忆", "importance": 0.1,
         "timestamp": now.timestamp()},
    ]
    plan = MemoryBackend.consolidate_plan(rows, now, min_importance=0.3,
                                          max_age_hours=720.0)
    expired_ids = [r["id"] for r in plan["expired"]]
    assert expired_ids == ["old"], f"仅超龄低重要度应过期, got {expired_ids}"
    assert "new" in [r["id"] for r in plan["kept"]]
    print("  OK test_plan_expire_old_low_importance")


def test_plan_unknown_age_not_expired():
    """timestamp 缺失/非法（≤0）→ 年龄未知，不过期（避免误删脏数据）。"""
    rows = [
        {"id": "no-ts", "text": "无时间戳记忆", "importance": 0.1, "timestamp": 0},
        {"id": "str-ts", "text": "字符串时间戳", "importance": 0.1,
         "timestamp": "2026-01-01T00:00:00+00:00"},
    ]
    plan = MemoryBackend.consolidate_plan(rows, _now(), min_importance=0.3,
                                          max_age_hours=1.0)
    assert plan["expired"] == [], f"年龄未知不应过期, got {plan['expired']}"
    print("  OK test_plan_unknown_age_not_expired")


def test_plan_no_false_positive_low_similarity():
    """相似度不足（不同话题）→ 不误判为重复。"""
    rows = [
        {"id": "a", "text": "哥哥喜欢喝美式咖啡", "importance": 0.9, "timestamp": _now().timestamp()},
        {"id": "b", "text": "一起去过苏州旅行", "importance": 0.8, "timestamp": _now().timestamp()},
    ]
    plan = MemoryBackend.consolidate_plan(rows, _now(), sim_threshold=0.85)
    assert plan["demoted"] == [], f"不相似文本不应降权, got {plan['demoted']}"
    print("  OK test_plan_no_false_positive_low_similarity")


# ── Mem0Backend.consolidate 写回 ─────────────────────────

def test_consolidate_mem0_writeback():
    """写回：过期 → FakeMem0.delete；降权 → FakeMem0.update(metadata importance 减半)。"""
    now = _now()
    with tempfile.TemporaryDirectory() as td:
        dup_a = _row("哥哥喜欢喝美式咖啡，不加糖", importance=0.9, age_hours=1, mem_id="a")
        dup_b = _row("哥哥喜欢喝美式咖啡，不加糖", importance=0.5, age_hours=2, mem_id="b")
        old_low = _row("多年前的琐事", importance=0.1,
                       mem_id="old",
                       created=datetime.fromtimestamp(
                           (now - timedelta(days=40)).timestamp(),
                           tz=timezone.utc).isoformat())
        b = _fake_backend([dup_a, dup_b, old_low], Path(td))
        rep = b.consolidate()
        assert rep["ok"] and rep["available"]
        assert rep["demoted_ids"] == ["b"]
        assert rep["expired_ids"] == ["old"]
        assert "old" in b._m.deleted, f"过期记忆应被 delete, got {b._m.deleted}"
        updates = {mid: data for mid, data in b._m.updated}
        assert "b" in updates, f"降权记忆应 update, got {b._m.updated}"
        assert abs(updates["b"]["importance"] - 0.25) < 1e-9
    print("  OK test_consolidate_mem0_writeback")


def test_consolidate_dry_run_no_write():
    """dry_run=True → 只出计划，不触发 delete/update。"""
    now = _now()
    with tempfile.TemporaryDirectory() as td:
        dup_a = _row("哥哥喜欢喝美式咖啡", importance=0.9, age_hours=1, mem_id="a")
        dup_b = _row("哥哥喜欢喝美式咖啡", importance=0.5, age_hours=2, mem_id="b")
        b = _fake_backend([dup_a, dup_b], Path(td))
        rep = b.consolidate(dry_run=True)
        assert rep["dry_run"] and rep["demoted_ids"] == ["b"]
        assert b._m.deleted == [] and b._m.updated == []
    print("  OK test_consolidate_dry_run_no_write")


def test_consolidate_unavailable():
    """CHIGUO_MEM0_DISABLED=1 → 报告 available=False，不抛。"""
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    try:
        b = Mem0Backend(qdrant_path="/tmp/q", history_db="/tmp/h.db")
        rep = b.consolidate()
        assert rep["ok"] is False and rep["available"] is False
        assert "error" in rep
    finally:
        os.environ.pop("CHIGUO_MEM0_DISABLED", None)
    print("  OK test_consolidate_unavailable")


def test_consolidate_writeback_failure_degrades():
    """C1 写回失败降级：delete/update 抛异常 → 报告仍返回 ok:True（计划可用），
    失败条目不进 deleted/updated 但不致整体崩溃（Medium 3 加固）。"""
    class FailingMem0(FakeMem0):
        def delete(self, memory_id):
            raise RuntimeError("qdrant 写失败")

        def update(self, memory_id, text=None, metadata=None):
            raise RuntimeError("qdrant 写失败")

    now = _now()
    with tempfile.TemporaryDirectory() as td:
        dup_a = _row("哥哥喜欢喝美式咖啡", importance=0.9, age_hours=1, mem_id="a")
        dup_b = _row("哥哥喜欢喝美式咖啡", importance=0.5, age_hours=2, mem_id="b")
        old_low = _row("多年前的琐事", importance=0.1,
                       mem_id="old",
                       created=datetime.fromtimestamp(
                           (now - timedelta(days=40)).timestamp(),
                           tz=timezone.utc).isoformat())
        b = _fake_backend([dup_a, dup_b, old_low], Path(td))
        b._m = FailingMem0([dup_a, dup_b, old_low])
        rep = b.consolidate()
        assert rep["ok"] is True, f"写回失败不应崩, got {rep}"
        assert rep["demoted_ids"] == ["b"] and rep["expired_ids"] == ["old"], rep
    print("  OK test_consolidate_writeback_failure_degrades")


# ── daemon 空闲静默路径 _maybe_consolidate ───────────────

class FakeCooldown:
    def __init__(self, silent_h, last_consolidate=None):
        self._silent_h = silent_h
        self.consolidate_last_at = last_consolidate

    def silent_hours(self, now):
        return self._silent_h

    def get_consolidate_last_at(self):
        return self.consolidate_last_at

    def set_consolidate_last_at(self, value):
        self.consolidate_last_at = value


class FakeBridge:
    def __init__(self):
        self.calls = 0

    def consolidate(self, **kw):
        self.calls += 1
        return {"ok": True, "demoted": [], "expired": []}


class FakeState:
    def __init__(self, bridge, cooldown):
        self.memory_bridge = bridge
        self.cooldown = cooldown
        self.saves = 0

    def save(self):
        self.saves += 1
        return True


def _maybe_consolidate(cfg, bridge, cooldown):
    from chiguo_daemon import DecisionEngine
    eng = SimpleNamespace(
        config=cfg,
        state=FakeState(bridge, cooldown),
    )
    DecisionEngine._maybe_consolidate(eng, _now())
    return eng


def test_maybe_consolidate_disabled():
    """consolidate_enabled=False → 不调用 consolidate（默认关闭恒等）。"""
    bridge = FakeBridge()
    cooldown = FakeCooldown(100.0)
    eng = _maybe_consolidate({"memory": {"consolidate_enabled": False}},
                             bridge, cooldown)
    assert bridge.calls == 0 and eng.state.saves == 0
    print("  OK test_maybe_consolidate_disabled")


def test_maybe_consolidate_silent_too_low():
    """silent_h < consolidate_idle_silent_hours → 不触发。"""
    bridge = FakeBridge()
    cooldown = FakeCooldown(5.0)  # 沉默 5h < 24h
    eng = _maybe_consolidate({"memory": {"consolidate_enabled": True,
                                         "consolidate_idle_silent_hours": 24.0}},
                             bridge, cooldown)
    assert bridge.calls == 0
    assert eng.state.cooldown.consolidate_last_at is None
    print("  OK test_maybe_consolidate_silent_too_low")


def test_maybe_consolidate_interval_guard():
    """距上次巩固不足 min_interval → 不重复触发。"""
    bridge = FakeBridge()
    last = (_now() - timedelta(hours=1)).isoformat()  # 1h 前刚巩固过
    cooldown = FakeCooldown(100.0, last_consolidate=last)
    eng = _maybe_consolidate({"memory": {"consolidate_enabled": True,
                                         "consolidate_idle_silent_hours": 24.0,
                                         "consolidate_min_interval_hours": 168.0}},
                             bridge, cooldown)
    assert bridge.calls == 0
    print("  OK test_maybe_consolidate_interval_guard")


def test_maybe_consolidate_triggers_and_persists():
    """门控全过 → 调用 consolidate + 持久化 consolidate_last_at + save。"""
    bridge = FakeBridge()
    cooldown = FakeCooldown(100.0)  # 沉默 100h ≥ 24h，从未巩固
    eng = _maybe_consolidate({"memory": {"consolidate_enabled": True,
                                         "consolidate_idle_silent_hours": 24.0,
                                         "consolidate_min_interval_hours": 168.0}},
                             bridge, cooldown)
    assert bridge.calls == 1
    assert eng.state.cooldown.consolidate_last_at is not None
    assert eng.state.saves >= 1
    print("  OK test_maybe_consolidate_triggers_and_persists")
