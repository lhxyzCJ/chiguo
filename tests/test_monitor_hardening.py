#!/usr/bin/env python3
"""test_monitor_hardening.py — chiguo_monitor 加固测试

覆盖两个独立验证过的 bug：
1. state/break_state 文件是合法 JSON 但形状错误（[]/123/"x"）时，
   stats()/alerts()/health() 不得 AttributeError 崩溃（防御 _read_state 家族）。
2. 日志混入 ISO 格式时间条目（daemon compact 输出 datetime.now(CST).isoformat()，
   含 T/微秒/+08:00）时不再被静默丢弃：total_sends 正常计数，
   stats 输出暴露 unparsed_time_count 反映无法解析时间的条目数。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_monitor import ChiguoMonitor


def make_log_entry(action, **kwargs):
    """构造一条决策日志（与 test_monitor.py 同构）"""
    t = kwargs.pop("time", datetime.now(CST))
    entry = {
        "action": action,
        "state": {
            "emotion": kwargs.pop("emotion", {
                "loneliness": 15.0, "affection": 55.0,
                "anxiety": 40.0, "energy": 85.0, "tsundere_index": 70.0,
            }),
            "dominant_layer": kwargs.pop("layer", "shell"),
            "cooldown": {
                "messages_today": kwargs.pop("messages_today", 0),
                "silent_hours": kwargs.pop("silent_hours", 10.0),
                "minutes_since_last": kwargs.pop("minutes_since_last", 60.0),
                "messages_without_reply": kwargs.pop("mwr", 0),
                "can_send": True,
            },
            "time": t.strftime("%Y-%m-%d %H:%M"),
        },
    }
    if action == "send":
        entry["trigger"] = kwargs.pop("trigger", "lonely_low")
        entry["intensity"] = kwargs.pop("intensity", "soft")
    elif action == "idle":
        entry["reason"] = kwargs.pop("reason", "no_trigger")
    return entry


# ═══════════════════════════════════════════════════════════
# Bug 1: state 文件形状错误（合法 JSON 但非 dict）→ 不崩溃
# ═══════════════════════════════════════════════════════════

def test_state_file_bad_shape():
    """chiguo_state.json 内容为 []/123/"x" → stats/alerts/health 不抛异常"""
    for bad in ("[]", "123", '"x"'):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            log.write_text("")  # 空日志
            state = Path(td) / "state.json"
            state.write_text(bad)

            mon = ChiguoMonitor(str(log), str(state))

            # stats()：state_data.get("emotion")（stats() 内）不得 AttributeError
            s = mon.stats(days=7)
            assert s["activity"]["total_sends"] == 0
            assert s["emotions"]["current"] is None

            # alerts()：state.get("last_tick")（alerts() 内）不得 AttributeError
            a = mon.alerts()
            # 形状错误等同「无状态」→ 应产生 no_state 告警而非崩溃
            no_state = [x for x in a if x["type"] == "no_state"]
            assert len(no_state) == 1, f"expected no_state alert, got {[x['type'] for x in a]}"

            # health()：state.get(...) 全程不得 AttributeError
            h = mon.health()
            assert h["last_tick"] is None
    print("  OK test_state_file_bad_shape")


def test_break_state_file_bad_shape():
    """break_state.json 内容为 []/123/"x" → alerts() 不抛异常"""
    for bad in ("[]", "123", '"x"'):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "log.jsonl"
            log.write_text("")
            state = Path(td) / "state.json"
            state.write_text(json.dumps({
                "_version": 2,
                "last_tick": datetime.now(CST).isoformat(),
            }))
            break_state = Path(td) / "break_state.json"
            break_state.write_text(bad)

            # break_state_path 是第 3 个构造参数
            mon = ChiguoMonitor(str(log), str(state), str(break_state))
            a = mon.alerts()  # break_data.get("manual_override")（alerts() 内）不得 AttributeError
            assert isinstance(a, list)
            assert all(x["type"] != "manual_break_active" for x in a)
    print("  OK test_break_state_file_bad_shape")


# ═══════════════════════════════════════════════════════════
# Bug 2: ISO 时间条目不再静默丢弃 + unparsed_time_count 暴露
# ═══════════════════════════════════════════════════════════

def test_extract_time_iso_fallback():
    """_extract_time 支持 ISO 格式（含 T/微秒/时区），naive→CST 口径一致"""
    # daemon compact 风格：datetime.now(CST).isoformat()
    iso = datetime.now(CST).isoformat()
    assert "T" in iso and "+08:00" in iso
    dt = ChiguoMonitor._extract_time({"state": {"time": iso}})
    assert dt is not None, "ISO 时间应能解析"
    assert dt.utcoffset() == timedelta(hours=8)
    assert dt.tzinfo is not None

    # 顶层 time 字段同样支持 ISO
    dt2 = ChiguoMonitor._extract_time({"time": "2025-01-02T03:04:05+08:00"})
    assert dt2 is not None and dt2.hour == 3

    # 非 +08:00 时区 → 换算回 CST（与 _parse_msg_ts 口径一致）
    dt3 = ChiguoMonitor._extract_time({"state": {"time": "2025-01-02T03:04:05+00:00"}})
    assert dt3 is not None
    assert dt3.utcoffset() == timedelta(hours=8)
    assert dt3.hour == 11  # 03:04 UTC = 11:04 CST

    # naive ISO（无时区）→ 视为 CST
    dt4 = ChiguoMonitor._extract_time({"state": {"time": "2025-01-02T03:04:05"}})
    assert dt4 is not None and dt4.hour == 3 and dt4.utcoffset() == timedelta(hours=8)

    # 原有 naive 格式不受影响
    dt5 = ChiguoMonitor._extract_time({"state": {"time": "2025-01-02 03:04"}})
    assert dt5 is not None and dt5.hour == 3 and dt5.utcoffset() == timedelta(hours=8)

    # 无法解析 → None（不抛异常）
    assert ChiguoMonitor._extract_time({"state": {"time": "not-a-time"}}) is None
    assert ChiguoMonitor._extract_time({"state": {"time": 12345}}) is None
    assert ChiguoMonitor._extract_time({"time": ""}) is None
    assert ChiguoMonitor._extract_time({}) is None
    print("  OK test_extract_time_iso_fallback")


def test_iso_time_entries_counted_in_stats():
    """日志混入 ISO 时间条目 → total_sends 正常计数 + unparsed_time_count 反映未解析数"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "mixed.jsonl"
        t0 = datetime.now(CST) - timedelta(days=2)
        lines = []
        # 1) 正常 naive 格式 send
        lines.append(json.dumps(make_log_entry("send", trigger="morning", time=t0),
                                ensure_ascii=False))
        # 2) daemon compact 风格 ISO 格式 send（state.time 含 T/微秒/+08:00）
        iso_time = datetime.now(CST).isoformat()
        lines.append(json.dumps({
            "action": "send", "trigger": "memory", "intensity": "soft",
            "state": {"time": iso_time, "emotion": {}, "cooldown": {}},
        }, ensure_ascii=False))
        # 3) 顶层 time 字段为 ISO 的 send
        lines.append(json.dumps({
            "action": "send", "trigger": "lonely_low", "intensity": "soft",
            "time": datetime.now(CST).isoformat(),
        }, ensure_ascii=False))
        # 4) 完全无法解析时间的条目（无 time 字段）→ 计入 unparsed
        lines.append(json.dumps({"action": "send", "trigger": "lonely_low"}, ensure_ascii=False))
        # 5) 时间字段是垃圾字符串 → 计入 unparsed
        lines.append(json.dumps(make_log_entry("send", trigger="morning", time=t0) | {
            "state": {"time": "yesterday-ish"}, "time": None,
        }, ensure_ascii=False))
        log.write_text("\n".join(lines) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2,
                                     "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)  # 全部历史

        # ISO 条目不再被静默丢弃：3 个可解析 send 全部计数
        assert s["activity"]["total_sends"] == 3, f"got {s['activity']['total_sends']}"
        assert s["activity"]["by_trigger"]["memory"] == 1
        # 无法解析时间的条目数被暴露
        assert s["period"]["unparsed_time_count"] == 2, f"got {s['period']}"

        # 同样验证 days=7（since 过滤下计数语义不变：无法解析条目无法按时间过滤，仍计入）
        s7 = mon.stats(days=7)
        assert s7["activity"]["total_sends"] == 3
        assert s7["period"]["unparsed_time_count"] == 2
    print("  OK test_iso_time_entries_counted_in_stats")


def test_invalid_decision_count_in_stats():
    """B10: _iter_decisions 消费 validate_decision 返回值——非法决策行计入
    period.invalid_decision_count（不跳过不静默），合法行统计不受影响。
    注：make_log_entry 简化 fixture 缺 version/msg_id/context 会被 schema 判非法，
    故本条用补全必填字段的合规记录构造合法基线。"""
    def full_send(time_str):
        return {
            "action": "send", "trigger": "morning", "intensity": "soft",
            "version": "1.24", "msg_id": "m1", "context": {},
            "state": {
                "emotion": {"loneliness": 15.0, "affection": 55.0,
                            "anxiety": 40.0, "energy": 85.0, "tsundere_index": 70.0},
                "dominant_layer": "shell",
                "cooldown": {"messages_today": 0, "silent_hours": 10.0,
                             "minutes_since_last": 60.0,
                             "messages_without_reply": 0, "can_send": True},
                "time": time_str,
            },
        }

    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        t0 = datetime.now(CST) - timedelta(days=1)
        ts = t0.strftime("%Y-%m-%d %H:%M")
        lines = [
            json.dumps(full_send(ts), ensure_ascii=False),           # 合法 send
            json.dumps({"action": "explode", "state": {"time": ts}},  # action 不在枚举 → 非法
                       ensure_ascii=False),
            json.dumps({"state": {"time": ts}}, ensure_ascii=False),  # 缺 action → 非法
        ]
        log.write_text("\n".join(lines) + "\n")
        mon = ChiguoMonitor(str(log), str(Path(td) / "state.json"))
        s = mon.stats(days=0)
        assert s["period"]["invalid_decision_count"] == 2, f"got {s['period']}"
        assert s["period"]["total_entries"] == 1, f"got {s['period']}"
        assert s["activity"]["total_sends"] == 1
        # 窗口粒度：再次调用 stats 独立复位，不累积残留污染
        assert mon.stats(days=0)["period"]["invalid_decision_count"] == 2
    print("  OK test_invalid_decision_count_in_stats")


def test_invalid_decision_count_window_scope():
    """B10: invalid_decision_count 随 days 窗口过滤——历史非法行不计入窗口
    （计数在 since 过滤后发生，对齐 unparsed_time_count 窗口语义）。"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        old = (datetime.now(CST) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M")
        recent = datetime.now(CST).strftime("%Y-%m-%d %H:%M")
        lines = [
            json.dumps({"action": "explode", "state": {"time": old}},   # 30 天前非法 → 窗口外
                       ensure_ascii=False),
            json.dumps({"action": "explode", "state": {"time": recent}},  # 今日非法 → 窗口内
                       ensure_ascii=False),
        ]
        log.write_text("\n".join(lines) + "\n")
        mon = ChiguoMonitor(str(log), str(Path(td) / "state.json"))
        assert mon.stats(days=7)["period"]["invalid_decision_count"] == 1
        # days=0（全历史）→ 两条都计入
        assert mon.stats(days=0)["period"]["invalid_decision_count"] == 2
    print("  OK test_invalid_decision_count_window_scope")


def test_log_line_bad_shape_skipped():
    """R2: 日志行是合法 JSON 但非 dict（[]/\"x\"/123）→ 跳过，stats() 不崩溃。"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "mixed.jsonl"
        t0 = datetime.now(CST) - timedelta(days=1)
        lines = [
            json.dumps(make_log_entry("send", trigger="morning", time=t0)),
            "[]",          # 合法 JSON 非 dict
            '"x"',         # 合法 JSON 非 dict
            "123",         # 合法 JSON 非 dict
            json.dumps(make_log_entry("idle", trigger="no_trigger", time=t0)),
        ]
        log.write_text("\n".join(lines) + "\n")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))
        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["activity"]["total_sends"] == 1, f"非 dict 行应跳过: {s['activity']}"
        assert s["activity"]["total_idles"] == 1
        # alerts()/health() 走同一 _iter_decisions，同样不崩
        mon.alerts()
        mon.health()
    print("  OK test_log_line_bad_shape_skipped")


def test_state_file_invalid_utf8_falls_back():
    """R3: 状态文件含非法 UTF-8 字节 → 回退空 dict，stats/alerts/health 不崩溃。"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_bytes(b"\xff\xfe\x00broken\x80")  # 非法 UTF-8
        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["activity"]["total_sends"] == 0
        mon.alerts()
        mon.health()
        # 日志行同样：非法 UTF-8 行静默跳过（errors=replace 后 JSON 解析失败 → continue）
        t1 = datetime.now(CST) - timedelta(hours=1)
        log.write_bytes((json.dumps(make_log_entry("send", trigger="morning", time=t1)) + "\n").encode() + b'\xff\xfe\x00broken\n')
        s2 = mon.stats(days=0)
        assert s2["activity"]["total_sends"] == 1, f"非法 UTF-8 行应跳过: {s2}"
    print("  OK test_state_file_invalid_utf8_falls_back")
