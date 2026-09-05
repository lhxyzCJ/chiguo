#!/usr/bin/env python3
"""test_rotation.py — #390 JSONL 大小轮转 + 尾读优化回归测试。

覆盖：
a) max_size_mb 大小轮转触发（造大文件调 rotate_if_needed，断言被轮转 + 审计事件 kind == "size"）；
b) CLI 名单与 daemon 名单均含 chiguo_events.jsonl；
c) _MAX_TAIL_LINES 截断语义（写超量行 + since 窗口，断言只读尾部）。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

import chiguo_rotation as rot
from chiguo_monitor import ChiguoMonitor


def _write_cfg(td: Path, max_size_mb: int = 1) -> Path:
    cfg = td / "cfg.toml"
    cfg.write_text(
        "[logging]\nretention_months = 12\n"
        f"archive_dir = \"{td / 'archive'}\"\n"
        f"max_size_mb = {max_size_mb}\n"
    )
    return cfg


def test_size_rotation_triggers_and_logs_size_kind():
    """a) 超 max_size_mb 的当月文件触发大小轮转，审计事件 kind == "size"。

    文件 mtime 为当月（月轮转规则不会触发），体积 > max_size_mb（=1MB），
    因此只有大小分支能轮转它——断言归档产出 + 事件 kind 精确为 "size"。
    """
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        log = td / "big.jsonl"
        # ~1.2MB（> max_size_mb=1），mtime 保持当月
        log.write_bytes(b'{"action":"idle","state":{}}\n' * 40000)
        assert log.stat().st_size > 1024 * 1024
        cfg = _write_cfg(td, max_size_mb=1)

        rot.rotate_if_needed([str(log)], str(cfg))

        # 原路径被轮转为空文件（_rotate_one: rename + touch）
        assert log.exists() and log.stat().st_size == 0
        archived = list((td / "archive").glob("*-big.jsonl"))
        assert len(archived) == 1, f"大小轮转应产出归档: {list((td / 'archive').iterdir())}"

        # 审计事件 kind 精确为 size（经 conftest 隔离注入的临时事件文件）
        ev_path = rot._EVENTS_LOG_PATH
        assert ev_path is not None and Path(ev_path).exists()
        lines = [json.loads(l) for l in Path(ev_path).read_text().splitlines() if l.strip()]
        assert lines and lines[-1]["event"] == "rotation"
        assert lines[-1]["kind"] == "size", lines[-1]
    print("  OK test_size_rotation_triggers_and_logs_size_kind")


def test_size_rotation_skips_small_current_month_file():
    """a 补充：当月小文件不受大小分支影响（KEEP，无归档无事件）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        log = td / "small.jsonl"
        log.write_text('{"action":"idle","state":{}}\n')
        cfg = _write_cfg(td, max_size_mb=1)

        rot.rotate_if_needed([str(log)], str(cfg))

        assert log.read_text().strip() != "", "小文件不应被轮转"
        assert not (td / "archive").exists() or not list((td / "archive").iterdir())
    print("  OK test_size_rotation_skips_small_current_month_file")


def test_events_in_cli_and_daemon_rotation_lists():
    """b) CLI 名单（chiguo_rotation __main__）与 daemon 名单（decision/base）
    均含 chiguo_events.jsonl，否则常驻运行时 events 文件无界增长。"""
    cli_src = Path(rot.__file__).read_text()
    assert "chiguo_events.jsonl" in cli_src, "CLI 轮转名单缺失 chiguo_events.jsonl"

    base_src = (Path(rot.__file__).resolve().parent / "decision" / "base.py").read_text()
    assert "chiguo_events.jsonl" in base_src, "daemon 轮转名单缺失 chiguo_events.jsonl"
    print("  OK test_events_in_cli_and_daemon_rotation_lists")


def _entry(at: datetime) -> str:
    return json.dumps({"action": "idle", "state": {"time": at.isoformat()}},
                      ensure_ascii=False) + "\n"


def test_tail_lines_truncation_semantics():
    """c) 超 _MAX_TAIL_LINES 行 + since 窗口 → 只读尾部，窗口外旧行不可达。

    写（cap + 500）行旧记录 + 100 行窗口内记录；since 取 14 天窗口，
    断言返回恰为 100 行近期记录（旧行因截断/窗口停止而不可见）。
    """
    cap = ChiguoMonitor._MAX_TAIL_LINES
    assert cap > 0
    n_old, n_recent = cap + 500, 100
    now = datetime.now(CST)
    old_t = now - timedelta(days=30)
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        log = td / "decisions.jsonl"
        with open(log, "w", encoding="utf-8") as f:
            for _ in range(n_old):
                f.write(_entry(old_t))
            for _ in range(n_recent):
                f.write(_entry(now - timedelta(hours=1)))
        state = td / "state.json"
        state.write_text(json.dumps({"_version": 2}))

        mon = ChiguoMonitor(str(log), str(state))
        since = now - timedelta(days=14)
        got = list(mon._iter_decisions(since))

        assert len(got) == n_recent, f"应只读尾部 {n_recent} 行，实得 {len(got)}"
        for e in got:
            ts = mon._extract_time(e)
            assert ts is not None and ts >= since
    print("  OK test_tail_lines_truncation_semantics")
