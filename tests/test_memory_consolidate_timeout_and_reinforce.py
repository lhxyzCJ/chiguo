#!/usr/bin/env python3
"""test_memory_consolidate_timeout_and_reinforce.py — 盲区6 memory 串联（AUD-031）

Given: ops/engine_ops._maybe_consolidate / chiguo_concurrent.call_with_timeout / mem0_backend consolidate / note_recalled
When:  consolidate 超时 / hot-loop 推进 consolidate_last_at / recall_count 跨进程持久化
Then:  相对应分支均被覆盖
"""
import sys
import os
import tempfile
import time
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))
import ops.engine_ops as eng_mod


def _now():
    return datetime(2026, 6, 15, 14, 0, tzinfo=CST)


def test_consolidate_timeout_sets_last_at():
    """_maybe_consolidate 的 bridge.consolidate 超时 → 仍推进 consolidate_last_at（防 hot-loop）。"""
    from chiguo_daemon import DecisionEngine

    class SlowBridge:
        def consolidate(self, **kw):
            time.sleep(5)
            return {"ok": True}

    # 缩短超时以便测试：直接测共享助手 call_with_timeout（#397 收敛单源）
    from chiguo_concurrent import TIMEOUT, call_with_timeout
    assert call_with_timeout(lambda: time.sleep(0.5), timeout=0.05) is TIMEOUT, "超时应返回 TIMEOUT"

    # 真实 _maybe_consolidate 路径：slow bridge + 门控全开 → finally 推进 last_at
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "chiguo_proactive.toml"
        cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
        import tomllib
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(td)
        cfg["memory"]["mem0_qdrant_path"] = str(Path(td) / "no_qdrant")
        cfg["memory"]["mem0_history_db"] = str(Path(td) / "no_history.db")
        cfg["memory"]["consolidate_enabled"] = True
        cfg["memory"]["consolidate_idle_silent_hours"] = 0.0
        cfg["memory"]["consolidate_min_interval_hours"] = 0.0
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"

        # 用 SlowBridge 注入 engine
        from chiguo_daemon import DecisionEngine as DE
        eng = DE(str(cfg_path), str(Path(td) / "chiguo_decisions.jsonl"))
        eng.config = cfg
        eng.state.config = cfg

        # 临时把超时缩短
        orig_timeout = eng_mod._CONSOLIDATE_TIMEOUT_S
        eng_mod._CONSOLIDATE_TIMEOUT_S = 0.05
        eng.state.memory_bridge = SlowBridge()
        eng.state.cooldown.consolidate_last_at = None
        eng.state.cooldown.silent_hours = lambda now: 100.0  # 门控沉默足够

        try:
            eng._maybe_consolidate(_now())
        finally:
            eng_mod._CONSOLIDATE_TIMEOUT_S = orig_timeout

        assert eng.state.cooldown.consolidate_last_at is not None, "超时也应推进 last_at"


def test_consolidate_last_at_persists_even_on_failure():
    """consolidate 抛异常 → finally 仍推进 last_at（间隔门控防每 tick 重试）。"""
    from chiguo_daemon import DecisionEngine

    class FailBridge:
        def consolidate(self, **kw):
            raise RuntimeError("qdrant down")

    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "chiguo_proactive.toml"
        cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
        import tomllib
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(td)
        cfg["memory"]["mem0_qdrant_path"] = str(Path(td) / "no_qdrant")
        cfg["memory"]["mem0_history_db"] = str(Path(td) / "no_history.db")
        cfg["memory"]["consolidate_enabled"] = True
        cfg["memory"]["consolidate_idle_silent_hours"] = 0.0
        cfg["memory"]["consolidate_min_interval_hours"] = 0.0
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        eng = DecisionEngine(str(cfg_path), str(Path(td) / "chiguo_decisions.jsonl"))
        eng.config = cfg
        eng.state.config = cfg
        eng.state.memory_bridge = FailBridge()
        eng.state.cooldown.consolidate_last_at = None
        eng.state.cooldown.silent_hours = lambda now: 100.0

        eng._maybe_consolidate(_now())
        assert eng.state.cooldown.consolidate_last_at is not None


def test_call_with_timeout_success():
    from chiguo_concurrent import call_with_timeout
    assert call_with_timeout(lambda: 42, timeout=1.0) == 42


def test_call_with_timeout_exception_propagates():
    from chiguo_concurrent import call_with_timeout
    try:
        call_with_timeout(lambda: (_ for _ in ()).throw(RuntimeError("boom")), timeout=1.0)
        assert False, "应抛异常"
    except RuntimeError as e:
        assert "boom" in str(e)


def test_call_with_timeout_none_is_not_timeout():
    """被包装函数合法返回 None ≠ 超时（哨兵语义，#414-11 回归）。"""
    from chiguo_concurrent import TIMEOUT, call_with_timeout
    assert call_with_timeout(lambda: None, timeout=1.0) is not TIMEOUT
    assert call_with_timeout(lambda: None, timeout=1.0) is None


def test_recall_count_cross_process_persists():
    """note_recalled 的 recall_count 经 _persist_recall 合并语义跨进程可见（FakeMem0 记录）。"""
    from memory.mem0_backend import Mem0Backend

    class FakeMem0:
        def __init__(self):
            self.store = {}

        def get(self, memory_id):
            return self.store.get(memory_id, {"id": memory_id, "metadata": {}})

        def update(self, memory_id, text=None, metadata=None):
            cur = self.store.setdefault(memory_id, {"id": memory_id, "metadata": {}})
            if metadata:
                cur["metadata"].update(metadata)

        def search(self, q, filters=None, top_k=10):
            return {"results": []}

        def get_all(self, filters=None, top_k=100):
            return {"results": []}

        def add(self, messages, user_id=None, metadata=None):
            return {"results": [{"id": "new"}]}

    with tempfile.TemporaryDirectory() as td:
        b = Mem0Backend(qdrant_path=str(Path(td) / "q"), history_db=str(Path(td) / "h.db"),
                        reinforce_enabled=True, reinforce_bonus=0.1)
        b._available = True
        b._last_probe = time.time() + 3600
        fake = FakeMem0()
        fake.store["m1"] = {"id": "m1", "memory": "hello", "metadata": {"recall_count": 2}}
        b._m = fake
        # 第一次 recall → 3（2→3）
        b.note_recalled(["m1"])
        assert fake.store["m1"]["metadata"]["recall_count"] == 3, f"got {fake.store['m1']['metadata']}"
        # 第二次 → 4（跨进程持久化语义：基于存量 +1）
        b2 = Mem0Backend(qdrant_path=str(Path(td) / "q2"), history_db=str(Path(td) / "h2.db"),
                         reinforce_enabled=True, reinforce_bonus=0.1)
        b2._available = True
        b2._last_probe = time.time() + 3600
        b2._m = fake
        b2.note_recalled(["m1"])
        assert fake.store["m1"]["metadata"]["recall_count"] == 4


def test_finite_float_converges_to_cfg_float():
    """#406(b)：_finite_float 收敛至 chiguo_math.cfg_float 单源——旧语义保持：
    非数值/NaN/inf/负数 → 回退默认；合法正数 → 原值。"""
    import math
    from memory.mem0_backend import _finite_float
    d = 7.5
    assert _finite_float(3.25, d) == 3.25
    assert _finite_float("2.5", d) == 2.5
    assert _finite_float("bad", d) == d
    assert _finite_float(None, d) == d
    assert _finite_float(float("nan"), d) == d
    assert _finite_float(float("inf"), d) == d
    assert _finite_float(-1.0, d) == d
    assert _finite_float("-3", d) == d
    assert _finite_float(0.0, d) == 0.0, "0.0 是合法值（非负），不应回退默认"
    assert _finite_float([1], d) == d
    print("  OK test_finite_float_converges_to_cfg_float")
