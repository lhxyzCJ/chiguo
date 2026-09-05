#!/usr/bin/env python3
"""test_idle_estimators.py — #396 表驱动拆分回归：ESTIMATORS 覆盖全部 reason。

Given: 真实 DecisionEngine（临时目录隔离运行时文件）
When:  对每个 idle reason 调用 _estimate_next_check(now, reason)
Then:  8 个已入表 reason 返回预期形状的值；sleeping_guard/未知 reason 返回 None
"""
import os
import random
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

TMP_DIR: Path | None = None
TMP_TOML: Path | None = None


def setup():
    global TMP_DIR, TMP_TOML
    TMP_DIR = Path(tempfile.mkdtemp(prefix="chiguo_test_idle_est_"))
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{TMP_DIR / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{TMP_DIR / "no_history.db"}"', src)
    TMP_TOML = TMP_DIR / "chiguo_proactive_test.toml"
    TMP_TOML.write_text(src)


def teardown():
    global TMP_DIR
    if TMP_DIR is not None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR = None


@pytest.fixture(scope="module")
def engine():
    from chiguo_daemon import DecisionEngine
    path = setup()
    try:
        eng = DecisionEngine(
            config_path=str(TMP_TOML),
            log_path=str(TMP_DIR / "chiguo_decisions.jsonl"),
        )
        yield eng
    finally:
        teardown()


def now():
    return datetime(2026, 6, 15, 23, 30, tzinfo=CST)


def test_estimators_table_shape(engine):
    """ESTIMATORS 为 dict，恰好覆盖 8 个 reason（user_sleeping/user_busy 共用一 fn）。"""
    assert isinstance(engine.ESTIMATORS, dict)
    assert set(engine.ESTIMATORS) == {
        "min_interval", "low_energy", "quiet_hours", "daily_limit",
        "no_trigger", "busy_suppressed", "user_sleeping", "user_busy",
    }


def test_min_interval(engine):
    s = engine.state
    n = now()
    s.cooldown.last_message_at = (n - timedelta(minutes=10)).isoformat()
    nxt = engine._estimate_next_check(n, "min_interval")
    assert nxt is not None
    assert datetime.fromisoformat(nxt) > n
    s.cooldown.last_message_at = None
    assert engine._estimate_next_check(n, "min_interval") is None


def test_low_energy(engine):
    s = engine.state
    n = now()
    s.emotion.energy = 5
    assert engine._estimate_next_check(n, "low_energy") is not None
    s.emotion.energy = 50
    assert engine._estimate_next_check(n, "low_energy") is None


def test_quiet_hours(engine):
    s = engine.state
    n = now()
    s.cooldown.set_quiet_window(22, 8)
    nxt = engine._estimate_next_check(n, "quiet_hours")
    assert nxt is not None
    assert datetime.fromisoformat(nxt) == datetime(2026, 6, 16, 8, 2, tzinfo=CST)


def test_daily_limit(engine):
    nxt = engine._estimate_next_check(now(), "daily_limit")
    assert nxt is not None
    assert datetime.fromisoformat(nxt) == datetime(2026, 6, 16, 8, 5, tzinfo=CST)


def test_no_trigger(engine):
    n = now()
    nxt = engine._estimate_next_check(n, "no_trigger")
    assert nxt is not None
    assert datetime.fromisoformat(nxt) > n


def test_busy_suppressed(engine):
    s = engine.state
    n = now()
    s.cooldown.busy_suppress_until = (n + timedelta(hours=1)).isoformat()
    assert engine._estimate_next_check(n, "busy_suppressed") == s.cooldown.busy_suppress_until
    s.cooldown.busy_suppress_until = None
    assert engine._estimate_next_check(n, "busy_suppressed") is None


def test_user_sleeping_or_busy(engine):
    random.seed(42)
    n = now()
    for reason in ("user_sleeping", "user_busy"):
        nxt = engine._estimate_next_check(n, reason)
        assert nxt is not None
        delta = (datetime.fromisoformat(nxt) - n).total_seconds()
        assert 3600 <= delta <= 7200


def test_sleeping_guard_and_unknown_return_none(engine):
    n = now()
    assert engine._estimate_next_check(n, "sleeping_guard") is None
    assert engine._estimate_next_check(n, "state_save_failed") is None
    assert engine._estimate_next_check(n, "bogus_reason") is None
