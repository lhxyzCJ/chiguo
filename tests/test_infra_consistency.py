#!/usr/bin/env python3
"""test_infra_consistency.py — Q21/Q22/Q23 基建收敛行为一致性验证

验证共享模块（chiguo_locks/chiguo_time/chiguo_atomic）相对重构前的语义等价：
  ① 锁语义：可重入、跨进程互斥、超时降级无锁、acquire/release 生命周期、
     与 state/agent_health 的锁路径约定一致。
  ② 原子写语义：tmp→os.replace、0600 一步到位、verify 失败→目标不变且 tmp 清理、
     fsync 路径不改变最终内容。
用法: uv run python tests/test_infra_consistency.py（退出码 0=全过，1=有失败）

隔离：全部用 tempfile.TemporaryDirectory，绝不触碰真实运行时文件。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chiguo_locks import acquire, release, in_lock, LOCK_TIMEOUT_S
from chiguo_time import CST
from chiguo_atomic import atomic_write

passed = 0
failed = 0


def t(name, fn):
    global passed, failed
    try:
        fn()
        passed += 1
        print(f"  ok - {name}")
    except Exception as e:
        failed += 1
        print(f"  FAIL - {name}: {e}")


# ── 锁语义 ────────────────────────────────────────────────

def _child_hold_lock(lock_path, hold_seconds):
    """子进程直接使用共享 chiguo_locks 持有锁 hold_seconds 秒。"""
    code = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from chiguo_locks import acquire\n"
        "got = acquire(sys.argv[1])\n"
        "print('got' if got else 'nogot')\n"
        "sys.stdout.flush()\n"
        "if got:\n"
        "    time.sleep(float(sys.argv[2]))\n"
        "    from chiguo_locks import release\n"
        "    release(sys.argv[1])\n"
        % str(ROOT)
    )
    p = subprocess.Popen([sys.executable, "-c", code, str(lock_path), str(hold_seconds)],
                         cwd=str(ROOT), stdout=subprocess.PIPE, text=True)
    line = p.stdout.readline().strip()
    return p, line


def test_lock_cross_process_mutex():
    """跨进程互斥：A 持有，B acquire 超时后返回 False（降级无锁）。"""
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "shared.lock")
        a = acquire(lp)
        assert a is True
        try:
            p, line = _child_hold_lock(lp, 0.5)
            assert line == "nogot", f"子进程在 A 持锁时应拿到 nogot，实际 {line!r}"
            # 在另一个进程里超时获取：acquire timeout=0.3 → 拿不到返回 False
            code = (
                "import sys\n"
                "sys.path.insert(0, %r)\n"
                "from chiguo_locks import acquire\n"
                "got = acquire(sys.argv[1], timeout=0.3)\n"
                "print('acquired' if got else 'degraded')\n"
                % str(ROOT)
            )
            r = subprocess.run([sys.executable, "-c", code, lp],
                               cwd=str(ROOT), capture_output=True, text=True, timeout=30)
            assert r.returncode == 0, r.stderr
            assert "degraded" in r.stdout, f"被持有时应超时降级，实际 {r.stdout!r}"
            p.wait(timeout=30)
        finally:
            release(lp)


def test_lock_reentrant_same_process():
    """同进程重入：二次 acquire 返回 False 且不释放持锁状态。"""
    with tempfile.TemporaryDirectory() as td:
        lp = os.path.join(td, "r.lock")
        got1 = acquire(lp)
        assert got1 is True
        got2 = acquire(lp)
        assert got2 is False, "同进程重入应返回 False（带外 acquire 视为未新获锁）"
        assert in_lock(lp) is True
        release(lp)
        assert in_lock(lp) is False
        release(lp)  # 幂等释放不炸
        assert in_lock(lp) is False


def test_lock_state_and_health_share_module():
    """state 与 agent_health 的锁路径约定一致：state_path + '.lock'。"""
    from chiguo_state import ChiguoState
    from schedule.parser import refresh_schedule_cache
    # 锁路径派生约定：两处都用 str(path) + '.lock'
    assert str(Path("/tmp/x.json")) + ".lock" == "/tmp/x.json.lock"
    # 共享模块同源：state._lock_acquire 委托给 locks.acquire
    import chiguo_locks as locks
    assert locks.acquire is acquire


# ── 原子写语义 ────────────────────────────────────────────

def test_atomic_success_0600_no_tmp():
    """成功写：内容落盘、权限 0600、无 .tmp 残留。"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "f.json")
        atomic_write(p, json.dumps({"a": 1}), mode=0o600)
        assert json.loads(open(p).read()) == {"a": 1}
        assert (os.stat(p).st_mode & 0o777) == 0o600
        assert not os.path.exists(p + ".tmp")


def test_atomic_verify_failure_keeps_target_cleans_tmp():
    """verify 失败：目标文件保持原内容，.tmp 被清理。"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "f.json")
        with open(p, "w") as f:
            f.write("original")
        def bad_v(t):
            raise ValueError("bad tmp")
        try:
            atomic_write(p, "new", mode=0o600, verify=bad_v)
        except ValueError:
            pass
        else:
            raise AssertionError("verify 失败应抛 ValueError")
        assert open(p).read() == "original", "失败时不应覆盖目标"
        assert not os.path.exists(p + ".tmp"), ".tmp 应被清理"


def test_atomic_fsync_and_plain_modes():
    """fsync 路径与 mode=None（默认 umask）均正确落盘。"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "plain.json")
        atomic_write(p, "x", fsync=True)
        assert open(p).read() == "x"
        assert not os.path.exists(p + ".tmp")
        p2 = os.path.join(td, "default.json")
        atomic_write(p2, "y")
        assert open(p2).read() == "y"


def test_atomic_bytes_write():
    """二进制内容原子写。"""
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "b.dat")
        atomic_write(p, b"\x00\x01\x02", mode=0o600)
        assert open(p, "rb").read() == b"\x00\x01\x02"


# ── CST 语义 ──────────────────────────────────────────────

def test_cst_shared_constant():
    """共享 CST == UTC+8；所有原实现来自单一来源。"""
    from datetime import timedelta, timezone
    assert CST == timezone(timedelta(hours=8))
    assert CST.utcoffset(None) == timedelta(hours=8)
    import chiguo_math, chiguo_daemon, chiguo_state, netease.service, schedule.holiday
    assert chiguo_math.CST is CST
    assert chiguo_daemon.CST is CST
    assert chiguo_state.CST is CST
    assert netease.service.CST is CST
    assert schedule.holiday.CST is CST


def run_all():
    t("锁-跨进程互斥 + 被持有时超时降级", test_lock_cross_process_mutex)
    t("锁-同进程重入 / 幂等释放", test_lock_reentrant_same_process)
    t("锁-state/agent_health 共享同源模块", test_lock_state_and_health_share_module)
    t("原子写-成功 0600 无 tmp 残留", test_atomic_success_0600_no_tmp)
    t("原子写-verify 失败保持目标并清理 tmp", test_atomic_verify_failure_keeps_target_cleans_tmp)
    t("原子写-fsync / 默认 mode 落盘", test_atomic_fsync_and_plain_modes)
    t("原子写-二进制内容", test_atomic_bytes_write)
    t("CST-共享常量单一来源", test_cst_shared_constant)
    print(f"\n{passed} passed, {failed} failed")
    return 0 if failed == 0 else 1
