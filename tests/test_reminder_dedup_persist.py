#!/usr/bin/env python3
"""test_reminder_dedup_persist.py — Q7 (#260) reminder 去重标记跨进程持久化

问题：chiguo_daemon.py 在 memory dict 上原地写 last_triggered_at 只对当进程
生效；chiguo_state.py save() payload 不含 memories → cron 每 15 分钟新进程
load 后标记丢失，窗口内多评估路径重复触发。

修复：去重标记并入 state JSON（memory_dedup 字段，仅存标记不含 memories 全文），
load 读回回写到 self.memories；daemon 原地标记改走公开 API mark_memory_triggered。

验收：
  ① 跨进程测试——两个进程先后评估同一 reminder，第二次不触发
  ② state roundtrip 保留标记
"""

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tomllib  # noqa: E402

from chiguo_trigger import _memory_should_trigger  # noqa: E402
from chiguo_state import ChiguoState, _memory_dedup_key  # noqa: E402

CST = timezone(timedelta(hours=8))
PROJ_ROOT = Path(__file__).resolve().parents[1]

# 模拟 cron 每次评估起新进程的 worker 片段（经 subprocess 真实跨进程执行）。
# role=first：加载记忆 → 确认可触发 → 经公开 API 标记并 save。
# role=second：新进程加载（state.read + memory_dedup 回写）→ 标记保留且不再触发。
_WORKER = r"""
import sys, os, json, tomllib
from pathlib import Path
from datetime import datetime, timezone, timedelta

base, role = Path(sys.argv[1]), sys.argv[2]
with open(base / "probe.toml", "rb") as f:
    cfg = tomllib.load(f)
cfg["_base_dir"] = str(base)
CST = timezone(timedelta(hours=8))
import chiguo_state, chiguo_trigger
from chiguo_state import ChiguoState
from chiguo_trigger import _memory_should_trigger

now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)  # 周一白天，静默窗口(0-8)外
state = ChiguoState(cfg)
reminders = [m for m in state.memories
             if isinstance(m, dict) and m.get("type") == "reminder"]
if not reminders:
    sys.exit("no reminder memory found")
rem = reminders[0]

if role == "first":
    # 首进程：评估窗口内可触发（trigger_at=13:55，now=14:00，Δ=300s ∈ [0,600)）
    assert _memory_should_trigger(rem, now) is True, "first process should trigger"
    # daemon 发送确认后改走公开 API 标记（原地 dict + 去重缓存同时更新）
    state.mark_memory_triggered(rem, now)
    assert rem.get("last_triggered_at") == now.isoformat(), rem
    assert state.save(), "first process save() failed"
    print("OK1")
elif role == "second":
    # 第二个（新）进程：load 应读回标记并回写到记忆 → 不再触发
    assert rem.get("last_triggered_at") == now.isoformat(), \
        f"second process must restore marker via state roundtrip, got {rem!r}"
    assert _memory_should_trigger(rem, now) is False, \
        "second process must NOT re-trigger the same reminder"
    print("OK2")
else:
    sys.exit(f"unknown role: {role!r}")
"""


def _setup_base() -> Path:
    """临时 base_dir：写入 reminder 记忆文件 + 探测 toml（_base_dir 注入）。"""
    base = Path(tempfile.mkdtemp(prefix="chiguo_q7_persist_"))
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "data" / "chiguo_memories.json").write_text(json.dumps([
        {"type": "reminder", "trigger_at": "2026-06-15T13:55", "content": "喝水"}
    ]))
    src = (PROJ_ROOT / "chiguo_proactive.toml").read_text()
    # 记忆库路径隔离到临时目录，防连到生产 qdrant/历史库
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{base / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{base / "no_history.db"}"', src)
    (base / "probe.toml").write_text(src)
    return base


def _run_worker(base: Path, role: str) -> str:
    env = dict(os.environ)
    env["CHIGUO_MEM0_DISABLED"] = "1"  # 确定性不可用：mem0 记忆候选不掺入
    proc = subprocess.run(
        ["uv", "run", "python", "-c", _WORKER, str(base), role],
        cwd=str(PROJ_ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"worker[{role}] failed rc={proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc.stdout.strip()


def test_reminder_dedup_cross_process_two_processes():
    """验收①：两个独立子进程先后评估同一 reminder，第二次不再触发。

    首进程评估可触发 → 发送标记（mark_memory_triggered + save 落盘 memory_dedup）；
    第二个进程是全新 Python 进程（新解释器 + 新 ChiguoState），从同一 base_dir
    load 记忆 + state，读回标记 → _memory_should_trigger 返回 False。
    """
    base = _setup_base()
    try:
        out1 = _run_worker(base, "first")
        out2 = _run_worker(base, "second")
        assert out1 == "OK1", out1
        assert out2 == "OK2", out2
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    print("  OK test_reminder_dedup_cross_process_two_processes")


def test_reminder_dedup_state_roundtrip_preserves_marker():
    """验收②：state roundtrip 保留标记。

    同进程内多次 _load/save 往返，last_triggered_at 与 memory_dedup 均保留；
    且记忆文件内容不被 state 改写（chiguo_memories.json 仍是内容唯一事实源）。
    """
    base = _setup_base()
    try:
        with open(base / "probe.toml", "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(base)
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)

        s = ChiguoState(dict(cfg))
        rem = s.memories[0]
        s.mark_memory_triggered(rem, now)
        assert s.save(), "first save failed"

        # 新实例（等价新进程）加载 → 标记读回
        s2 = ChiguoState(dict(cfg))
        rem2 = s2.memories[0]
        assert rem2.get("last_triggered_at") == now.isoformat(), rem2
        assert _memory_should_trigger(rem2, now) is False
        assert s2.state_path.exists()

        # 磁盘 state payload 携带 memory_dedup（内容键 → last_triggered_at）
        disk = json.loads(s2.state_path.read_text())
        key = _memory_dedup_key({k: v for k, v in rem2.items()
                                 if k != "last_triggered_at"})
        assert disk.get("memory_dedup", {}).get(key) == now.isoformat(), disk

        # 记忆文件本身不带标记（不可变事实源；标记只存 state）
        mem_file = json.loads((base / "data" / "chiguo_memories.json").read_text())
        assert "last_triggered_at" not in mem_file[0], mem_file
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    print("  OK test_reminder_dedup_state_roundtrip_preserves_marker")


def test_reminder_dedup_empty_payload_stays_clean():
    """无标记时不写 memory_dedup 字段（与 bayesian 同策略，状态文件保持干净）。"""
    base = _setup_base()
    try:
        with open(base / "probe.toml", "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(base)
        s = ChiguoState(dict(cfg))
        assert s.save(), "save should succeed without markers"
        disk = json.loads(s.state_path.read_text())
        assert "memory_dedup" not in disk, "empty marker map must not be persisted"
    finally:
        import shutil
        shutil.rmtree(base, ignore_errors=True)
    print("  OK test_reminder_dedup_empty_payload_stays_clean")


if __name__ == "__main__":
    test_reminder_dedup_cross_process_two_processes()
    test_reminder_dedup_state_roundtrip_preserves_marker()
    test_reminder_dedup_empty_payload_stays_clean()
    print("\ntest_reminder_dedup_persist.py: ALL PASS")
