#!/usr/bin/env python3
"""test_daemon_ntp_forward_cap.py — 盲区1 daemon NTP 前跳封顶（AUD-029）

Given: decision/core.py:30 REBOOT_ELAPSED_CAP_H=0.5h + _tick 三段封顶
When:  monotonic 回退 / wall_anchor 损坏 / 壁钟前跳 6h / normal 路径
Then:  elapsed 分别被 cap 0.5h / 不加封顶 / min(elapsed_real) / 不变；damp 24h 分支独立
"""
import os
import sys
import tempfile
import time
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

import decision.core as core_mod
from chiguo_daemon import DecisionEngine

REBOOT_CAP_H = 0.5


def _make_engine(tmp: str, now: datetime) -> DecisionEngine:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    eng = DecisionEngine(str(cfg_path), str(Path(tmp) / "chiguo_decisions.jsonl"))
    eng.state.cooldown.last_message_at = (now - timedelta(hours=6)).isoformat()
    eng.state.cooldown.last_user_message_at = None
    eng.state.last_tick = (now - timedelta(hours=6)).isoformat()
    eng.state.mono_anchor = None
    eng.state.wall_anchor = None
    eng.state.save()
    return eng


def _capture_tick_hours(eng: DecisionEngine, now: datetime, monkey_mono: float, wall_anchor: str | None, mono_anchor: float | None):
    """劫持 state.tick 捕获传入 hours，返回捕获值（None 表示未调用）。"""
    captured = {}
    orig_tick = eng.state.tick

    def _cap(hours, tick_now):
        captured["hours"] = hours

    eng.state.tick = _cap
    # 直接设内存锚点，不经 save/load 避免 last_tick 被污染
    eng.state.mono_anchor = mono_anchor
    eng.state.wall_anchor = wall_anchor
    with mock.patch.object(time, "monotonic", return_value=monkey_mono):
        eng._monotonic_at_save = 0.0
        eng._tick(now)
    eng.state.tick = orig_tick
    return captured.get("hours")


def test_ntp_const_exists():
    """常量 REBOOT_ELAPSED_CAP_H 存在且为 0.5h。"""
    assert hasattr(core_mod, "REBOOT_ELAPSED_CAP_H")
    assert core_mod.REBOOT_ELAPSED_CAP_H == 0.5


def test_reboot_cap_0_5h_when_mono_regresses():
    """monotonic 回退（mono_anchor > current）且 wall 6h → cap 0.5h。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        eng = _make_engine(td, now)
        wall_anchor = (now - timedelta(hours=6)).isoformat()
        hours = _capture_tick_hours(eng, now, monkey_mono=10.0, wall_anchor=wall_anchor, mono_anchor=1000.0)
        assert hours is not None, "应触发 tick"
        assert abs(hours - REBOOT_CAP_H) < 1e-9, f"回退域应 cap 0.5h, got {hours}"


def test_ntp_forward_min_elapsed_real():
    """NTP 前跳：wall 6h 但 monotonic 仅 0.2h → min(elapsed_real) 封顶。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        eng = _make_engine(td, now)
        wall_anchor = (now - timedelta(hours=6)).isoformat()
        mono_anchor = 100.0
        current = mono_anchor + 0.2 * 3600
        hours = _capture_tick_hours(eng, now, monkey_mono=current, wall_anchor=wall_anchor, mono_anchor=mono_anchor)
        assert hours is not None
        assert abs(hours - 0.2) < 1e-6, f"NTP 前跳应封顶到 elapsed_real 0.2h, got {hours}"


def test_wall_anchor_corrupt_no_cap():
    """wall_anchor 损坏（非法 ISO）→ 视为无锚点，不加封顶（6h 全量）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        eng = _make_engine(td, now)
        hours = _capture_tick_hours(eng, now, monkey_mono=99999.0, wall_anchor="not-a-datetime", mono_anchor=100.0)
        assert hours is not None
        assert abs(hours - 6.0) < 1e-9, f"损坏锚点应无 cap 6h, got {hours}"


def test_wall_anchor_none_no_cap():
    """wall_anchor=None → 无锚点，不加封顶。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        eng = _make_engine(td, now)
        hours = _capture_tick_hours(eng, now, monkey_mono=99999.0, wall_anchor=None, mono_anchor=100.0)
        assert hours is not None
        assert abs(hours - 6.0) < 1e-9, f"None 锚点应无 cap 6h, got {hours}"


def test_damp_over_24h():
    """elapsed 30h → dampened = 24 + (30-24)*0.5 = 27h（无 NTP 干扰）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        eng = _make_engine(td, now)
        eng.state.cooldown.last_message_at = (now - timedelta(hours=30)).isoformat()
        eng.state.last_tick = (now - timedelta(hours=30)).isoformat()
        eng.state.mono_anchor = None
        eng.state.wall_anchor = None
        captured = {}

        def _cap(hours, tick_now):
            captured["hours"] = hours

        orig = eng.state.tick
        eng.state.tick = _cap
        eng._monotonic_at_save = 0.0
        with mock.patch.object(time, "monotonic", return_value=99999.0):
            eng._tick(now)
        eng.state.tick = orig
        assert abs(captured["hours"] - 27.0) < 1e-9, f"damp 30h→27h, got {captured['hours']}"


def test_clock_backward_no_tick():
    """elapsed <0（last_time 在 now 之后）→ 不推进 tick。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        eng = _make_engine(td, now)
        eng.state.cooldown.last_message_at = (now + timedelta(hours=1)).isoformat()
        eng.state.last_tick = None
        eng.state.mono_anchor = None
        eng.state.wall_anchor = None
        called = []

        def _cap(hours, tick_now):
            called.append(hours)

        orig = eng.state.tick
        eng.state.tick = _cap
        eng._monotonic_at_save = 0.0
        eng._tick(now)
        eng.state.tick = orig
        assert called == [], f"时钟倒退不应 tick, got {called}"
