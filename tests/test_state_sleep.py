#!/usr/bin/env python3
"""test_state_sleep.py — 睡眠窗口/静默时段/加载防护回归测试（v11）

覆盖三个已确认缺陷:
  Bug1: _sleep_hours_in_range 跨午夜窗口(qe<qs)漏算"start 落在当天收尾段 [0:00, qe)"的场景
  Bug2: 加载路径对 emotion/cooldown 数值字段无类型校验(字符串 → clamp/can_send TypeError)
  Bug3: set_quiet_window / _sync_quiet_window 无 0-23 值域校验(quiet_start=24 → day.replace ValueError)
"""

import json
import os
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState, CooldownState


# ── Bug1: 跨午夜窗口收尾段漏算 ──────────────────────────


def _cd(qs: int, qe: int) -> CooldownState:
    c = CooldownState()
    c.set_quiet_window(qs, qe)
    return c


def test_sleep_hours_cross_midnight_start_in_main_segment():
    """窗口 22-8:start=昨晚 23:00(主段) → 今早 9:00 → 9h(整段在窗口内)"""
    c = _cd(22, 8)
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 30, 23, 0, tzinfo=CST),
        datetime(2026, 7, 31, 9, 0, tzinfo=CST),
    )
    assert abs(got - 9.0) < 1e-9, got
    print("  OK test_sleep_hours_cross_midnight_start_in_main_segment")


def test_sleep_hours_cross_midnight_start_in_early_tail():
    """窗口 13-10(跨午夜):start=凌晨 6:30(昨日窗口收尾段) → 9:30 → 3h(旧实现返回 0)"""
    c = _cd(13, 10)
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 31, 6, 30, tzinfo=CST),
        datetime(2026, 7, 31, 9, 30, tzinfo=CST),
    )
    assert abs(got - 3.0) < 1e-9, got
    print("  OK test_sleep_hours_cross_midnight_start_in_early_tail")


def test_sleep_hours_cross_midnight_two_nights():
    """窗口 13-10:跨两夜 前天 23:00 → 今天 9:00 = 前天尾段 11h + 昨日主段 20h = 31h"""
    c = _cd(13, 10)
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 29, 23, 0, tzinfo=CST),
        datetime(2026, 7, 31, 9, 0, tzinfo=CST),
    )
    assert abs(got - 31.0) < 1e-9, got
    print("  OK test_sleep_hours_cross_midnight_two_nights")


def test_sleep_hours_cross_midnight_tail_plus_main():
    """窗口 13-10:start=昨天 6:30(收尾段) 跨两夜 → 昨日 3.5h + 今日主段 20h = 23.5h"""
    c = _cd(13, 10)
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 30, 6, 30, tzinfo=CST),
        datetime(2026, 7, 31, 9, 0, tzinfo=CST),
    )
    assert abs(got - 23.5) < 1e-9, got
    print("  OK test_sleep_hours_cross_midnight_tail_plus_main")


def test_sleep_hours_cross_midnight_boundary_at_qe():
    """窗口 13-10:start 恰在 qe=10:00(收尾段之外) → 0h;到 14:00 → 仅主段 1h"""
    c = _cd(13, 10)
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 31, 10, 0, tzinfo=CST),
        datetime(2026, 7, 31, 12, 0, tzinfo=CST),
    )
    assert abs(got - 0.0) < 1e-9, got
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 31, 10, 0, tzinfo=CST),
        datetime(2026, 7, 31, 14, 0, tzinfo=CST),
    )
    assert abs(got - 1.0) < 1e-9, got
    print("  OK test_sleep_hours_cross_midnight_boundary_at_qe")


def test_sleep_hours_normal_window_regression():
    """回归:非跨午夜窗口行为不变(7:00→10:00 = 1h;23:00→次日 10:00 = 8h)"""
    c = _cd(0, 8)
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 31, 7, 0, tzinfo=CST),
        datetime(2026, 7, 31, 10, 0, tzinfo=CST),
    )
    assert abs(got - 1.0) < 1e-9, got
    got = c._sleep_hours_in_range(
        datetime(2026, 7, 30, 23, 0, tzinfo=CST),
        datetime(2026, 7, 31, 10, 0, tzinfo=CST),
    )
    assert abs(got - 8.0) < 1e-9, got
    print("  OK test_sleep_hours_normal_window_regression")


# ── Bug2: 加载路径数值字段类型强转 ──────────────────────


def _make_state(tmp: str) -> ChiguoState:
    """真实 toml 配置 + 临时目录锚定;mem0 指向不存在路径(确定性)"""
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return ChiguoState(cfg)


def test_load_string_numeric_fields_fall_back_defaults():
    """emotion/cooldown 数值字段为字符串/None → 加载不崩;非法串回退默认;可解析串强转"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        payload = {
            "_version": 10,
            "emotion": {"loneliness": "abc", "affection": "63",
                        "energy": None, "anxiety": "40.5"},
            "cooldown": {"messages_today": "abc", "messages_without_reply": "2",
                         "accumulated_lambda": "0.5"},
            "last_tick": "2026-07-31T10:00:00+08:00",
        }
        s.state_path.write_text(json.dumps(payload))
        s2 = _make_state(td)  # 加载不崩
        # 非法字符串/None → 回退 dataclass 默认
        assert s2.emotion.loneliness == 15.0, s2.emotion.loneliness
        assert s2.emotion.energy == 85.0, s2.emotion.energy
        assert s2.cooldown.messages_today == 0, s2.cooldown.messages_today
        # 可解析字符串 → 强转生效
        assert s2.emotion.affection == 63.0, s2.emotion.affection
        assert s2.emotion.anxiety == 40.5, s2.emotion.anxiety
        assert s2.cooldown.messages_without_reply == 2, s2.cooldown.messages_without_reply
        assert s2.cooldown.accumulated_lambda == 0.5, s2.cooldown.accumulated_lambda
        # 后续 clamp/can_send 不再 TypeError
        s2.emotion.clamp()
        s2.can_send(datetime(2026, 7, 31, 10, 0, tzinfo=CST))
    print("  OK test_load_string_numeric_fields_fall_back_defaults")


# ── Bug3: 睡眠窗口 0-23 值域校验 ────────────────────────


def test_set_quiet_window_invalid_values_fall_back_default():
    """set_quiet_window 值域校验:越界(24/-1)/非法字符串 → 回退 (0,8);合法值正常生效"""
    c = CooldownState()
    c.set_quiet_window(24, 8)
    assert c.quiet_window() == (0, 8), c.quiet_window()
    c.set_quiet_window(0, 24)
    assert c.quiet_window() == (0, 8), c.quiet_window()
    c.set_quiet_window(-1, 8)
    assert c.quiet_window() == (0, 8), c.quiet_window()
    c.set_quiet_window("abc", 5)
    assert c.quiet_window() == (0, 8), c.quiet_window()
    c.set_quiet_window(22, 8)
    assert c.quiet_window() == (22, 8), c.quiet_window()
    c.set_quiet_window("22", "8")  # 可强转字符串仍生效
    assert c.quiet_window() == (22, 8), c.quiet_window()
    print("  OK test_set_quiet_window_invalid_values_fall_back_default")


def test_sync_quiet_window_invalid_bucket_values_fall_back():
    """_sync_quiet_window 注入越界值(start=24) → cooldown 窗口回退 (0,8),睡眠计算不抛 ValueError"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s.circadian.weekday_quiet_start, s.circadian.weekday_quiet_end, s.circadian.weekday_confidence = 24, 8, 0.9
        s._sync_quiet_window(datetime(2026, 7, 27, 12, 0, tzinfo=CST))  # 周一 → weekday 桶,不崩
        assert s.cooldown.quiet_window() == (0, 8), s.cooldown.quiet_window()
        # 非法窗口下睡眠小时计算不再抛 ValueError
        got = s.cooldown._sleep_hours_in_range(
            datetime(2026, 7, 31, 6, 0, tzinfo=CST),
            datetime(2026, 7, 31, 10, 0, tzinfo=CST),
        )
        assert got >= 0.0
    print("  OK test_sync_quiet_window_invalid_bucket_values_fall_back")


def test_config_invalid_quiet_window_loads_and_runs():
    """config [schedule] quiet_start=24 → 加载回退 (0,8);silent_hours/can_send 不抛 ValueError"""
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "chiguo_proactive.toml"
        txt = Path("chiguo_proactive.toml").read_text()
        txt = txt.replace("quiet_start = 0", "quiet_start = 24")
        txt = txt.replace("quiet_end = 8", "quiet_end = 24")
        cfg_path.write_text(txt)
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(td)
        cfg["memory"]["mem0_qdrant_path"] = str(Path(td) / "no_qdrant")
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        s = ChiguoState(cfg)  # 加载不崩
        assert s.cooldown.quiet_window() == (0, 8), s.cooldown.quiet_window()
        now = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s.cooldown.last_user_message_at = (now - timedelta(hours=3)).isoformat()
        assert s.cooldown.silent_hours(now) >= 0.0
        s.can_send(now)  # 不抛
    print("  OK test_config_invalid_quiet_window_loads_and_runs")



