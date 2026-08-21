#!/usr/bin/env python3
"""test_form_guard.py — loop/cron 双形态运行期互斥守卫（Q28）

覆盖：①loop 形态启动时检测 cron（chiguo-tick flock）是否在跑 → 冲突拒启；
②cron 单次主动评估时检测 loop（chiguo_loop.pid 存活）是否在跑 → 冲突跳过；
③对方不存活（无锁 / 过期 pid / 缺失锁文件）→ 正常放行。

经 guard_mutual_form / startup_conflict 直接驱动，纯内存 + temp dir，零 LLM/网络，
不触真实运行时文件（chiguo_loop.pid / chiguo-tick.lock 均指向临时目录）。
"""

import fcntl
import io
import os
import re
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_daemon import (  # noqa: E402
    DecisionEngine,
    _run_passive,
    cron_form_active,
    guard_mutual_form,
    loop_form_active,
    startup_conflict,
)


class _HeldLock:
    """持有一个 cron 形态的 flock（模拟 chiguo-tick.sh 正在运行）。"""

    def __init__(self, lock_path: Path):
        os.makedirs(lock_path.parent, exist_ok=True)
        self._fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(self._fd, fcntl.LOCK_EX)

    def release(self):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = -1


def test_loop_form_blocked_when_cron_flock_held():
    """loop 启动：cron tick flock 被持有（cron 在跑）→ guard 冲突 + startup_conflict=1。"""
    with tempfile.TemporaryDirectory() as td:
        lock = Path(td) / "chiguo-tick.lock"
        held = _HeldLock(lock)
        try:
            os.environ["CHIGUO_LOCK_DIR"] = td
            assert cron_form_active() is True, "持有 flock 应判定 cron 活跃"
            conflict = guard_mutual_form(td, "loop")
            assert conflict is not None and "cron" in conflict, f"应报 cron 冲突: {conflict}"
            buf = io.StringIO()
            with redirect_stderr(buf):
                rc = startup_conflict(td, "loop")
            assert rc == 1, f"loop 冲突应返回 1, got {rc}"
            assert "拒绝启动 loop" in buf.getvalue(), buf.getvalue()
        finally:
            held.release()
            os.environ.pop("CHIGUO_LOCK_DIR", None)
    print("  OK test_loop_form_blocked_when_cron_flock_held")


def test_loop_form_allowed_when_cron_not_running():
    """loop 启动：无 cron flock 持有 → guard 无冲突 + startup_conflict=0。"""
    with tempfile.TemporaryDirectory() as td:
        os.environ["CHIGUO_LOCK_DIR"] = td  # 指向空目录，锁文件根本不存在
        assert cron_form_active() is False, "无锁文件应判定 cron 未运行"
        assert guard_mutual_form(td, "loop") is None
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = startup_conflict(td, "loop")
        assert rc == 0, f"loop 无冲突应返回 0, got {rc}"
        assert buf.getvalue() == "", "无冲突不应打印诊断"
        os.environ.pop("CHIGUO_LOCK_DIR", None)
    print("  OK test_loop_form_allowed_when_cron_not_running")


def test_cron_form_blocked_when_loop_alive():
    """cron 单次评估：chiguo_loop.pid 指向存活进程 → guard 冲突 + startup_conflict=2（跳过本 tick）。"""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "chiguo_loop.pid").write_text(str(os.getpid()))  # 本进程存活
        assert loop_form_active(td) is True, "存活 pid 应判定 loop 活跃"
        conflict = guard_mutual_form(td, "cron")
        assert conflict is not None and "loop" in conflict, f"应报 loop 冲突: {conflict}"
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = startup_conflict(td, "cron")
        assert rc == 2, f"cron 冲突应返回哨兵 2（跳过本 tick）got {rc}"
        assert rc != 0, "cron 冲突不得返回 0（否则守卫短路失效，会漏出决策）"
        assert "跳过本次单次主动评估" in buf.getvalue(), buf.getvalue()
    print("  OK test_cron_form_blocked_when_loop_alive")


def test_cron_form_allowed_when_loop_not_running():
    """cron 单次评估：loop pid 缺失 / 过期 → guard 无冲突 + startup_conflict=0（正常评估）。"""
    with tempfile.TemporaryDirectory() as td:
        # 无 pid 文件 → 正常
        assert loop_form_active(td) is False, "无 pid 应判定 loop 未运行"
        assert guard_mutual_form(td, "cron") is None
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = startup_conflict(td, "cron")
        assert rc == 0, f"cron 正常应返回 0, got {rc}"
        assert buf.getvalue() == "", "无冲突不应打印诊断"
        # 过期 pid（进程不存在）→ 正常
        (Path(td) / "chiguo_loop.pid").write_text("999999999")
        assert loop_form_active(td) is False, "过期 pid 应判定 loop 未运行"
        assert guard_mutual_form(td, "cron") is None
        # 损坏 pid → 正常
        (Path(td) / "chiguo_loop.pid").write_text("garbage")
        assert loop_form_active(td) is False, "损坏 pid 应判定 loop 未运行"
        assert guard_mutual_form(td, "cron") is None
    print("  OK test_cron_form_allowed_when_loop_not_running")


def test_unknown_form_raises():
    """非法形态值 → 抛 ValueError（防拼写错误静默放行）。"""
    with tempfile.TemporaryDirectory() as td:
        try:
            guard_mutual_form(td, "bogus")
        except ValueError:
            pass
        else:
            assert False, "未知形态应抛 ValueError"
    print("  OK test_unknown_form_raises")


def _make_temp_engine():
    """在临时目录复制 toml → 构造 DecisionEngine，确保 _base_dir 锚定到临时目录。

    隔离真实运行时文件：mem0 qdrant/history 路径改写进临时目录；所有 state/log
    都落在临时目录内，绝不动项目根（chiguo_loop.pid / *jsonl 均保持清理态）。
    """
    td = Path(tempfile.mkdtemp(prefix="chiguo_form_guard_"))
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{td / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{td / "no_history.db"}"', src)
    tmp_toml = td / "chiguo_proactive_test.toml"
    tmp_toml.write_text(src)
    engine = DecisionEngine(config_path=str(tmp_toml),
                            log_path=str(td / "chiguo_decisions.jsonl"))
    assert str(engine._base_dir) == str(td), \
        f"引擎 _base_dir 应锚定临时目录, got {engine._base_dir}"
    return td, engine


def test_daemon_entry_cron_conflict_skips_eval():
    """daemon cron 入口（_run_passive，与 main() 共用同一代码路径）：

    loop pid 存活 → 冲突判 skip → 直接 exit(0)、不调用 engine.evaluate()、
    stdout 无任何决策输出。回放 P0 复现场景（此前 cron 冲突返回 0 导致 evaluate
    照跑、stdout 漏出决策、可能双发送）。
    """
    td, engine = _make_temp_engine()
    try:
        (td / "chiguo_loop.pid").write_text(str(os.getpid()))  # loop 存活（本进程）
        assert loop_form_active(str(td)) is True
        # evaluate 若被调用 → 测试失败，证明守卫在 evaluate 前已短路
        def _boom():
            raise AssertionError("cron 冲突时不应调用 engine.evaluate()")
        engine.evaluate = _boom
        out = io.StringIO()
        try:
            with redirect_stderr(io.StringIO()), redirect_stdout(out):
                _run_passive(engine, compact=True)
        except SystemExit as e:
            assert e.code == 0, f"cron 冲突应 exit(0)（跳过本 tick），got {e.code}"
        else:
            assert False, "cron 冲突应触发 SystemExit(0)"
        assert out.getvalue() == "", \
            f"cron 冲突不应有任何 stdout 决策输出, got: {out.getvalue()!r}"
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    print("  OK test_daemon_entry_cron_conflict_skips_eval")


def test_daemon_entry_cron_no_conflict_evaluates():
    """daemon cron 入口：无 loop pid 冲突 → 正常调用 evaluate 并输出决策（不误伤放行）。"""
    td, engine = _make_temp_engine()
    try:
        def _idle():
            return {"action": "idle", "version": "test", "time": "2026-06-15T14:00:00+08:00"}
        engine.evaluate = _idle
        out = io.StringIO()
        with redirect_stderr(io.StringIO()), redirect_stdout(out):
            _run_passive(engine, compact=True)  # 无冲突 → 正常返回，不退出
        decision = out.getvalue().strip()
        assert decision != "", "无冲突时应输出决策"
        assert '"action": "idle"' in decision, f"应输出 idle 心跳, got: {decision}"
    finally:
        import shutil
        shutil.rmtree(td, ignore_errors=True)
    print("  OK test_daemon_entry_cron_no_conflict_evaluates")


if __name__ == "__main__":
    print("test_form_guard.py\n")
    tests = [
        test_loop_form_blocked_when_cron_flock_held,
        test_loop_form_allowed_when_cron_not_running,
        test_cron_form_blocked_when_loop_alive,
        test_cron_form_allowed_when_loop_not_running,
        test_unknown_form_raises,
        test_daemon_entry_cron_conflict_skips_eval,
        test_daemon_entry_cron_no_conflict_evaluates,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} form-guard tests passed.")
