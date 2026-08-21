#!/usr/bin/env python3
"""test_proactive_eval.py — D1 主动消息效果评估测试（[monitor].proactive_eval）

覆盖: 默认关闭恒等（不新增输出键）、开启后按 trigger 分组统计
sent/replied/reply_rate + overall、replied_within_hours 窗口判定、
发送前到达的 recv 不计为回复、窗口外回复不计。
零 LLM、流式 JSONL 解析（与 test_monitor.py 同构）。
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_monitor import ChiguoMonitor  # noqa: E402


def _emotion() -> dict:
    return {"loneliness": 15.0, "affection": 55.0, "anxiety": 40.0,
            "energy": 85.0, "tsundere_index": 70.0}


def _send_entry(t: datetime, trigger: str) -> dict:
    return {
        "action": "send",
        "trigger": trigger,
        "intensity": "soft",
        "state": {"time": t.strftime("%Y-%m-%d %H:%M"),
                  "emotion": _emotion(),
                  "cooldown": {"messages_without_reply": 1}},
    }


def _recv_entry(t: datetime) -> dict:
    return {
        "action": "recv",
        "state": {"time": t.strftime("%Y-%m-%d %H:%M"),
                  "emotion": _emotion(),
                  "cooldown": {}},
    }


def _write_entries(log: Path, entries: list[dict]):
    log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))


def _monitor(td: Path, proactive_eval: bool = False,
             replied_within_hours: float = 24.0) -> ChiguoMonitor:
    cfg = Path(td) / "chiguo_proactive.toml"
    cfg.write_text(f"[monitor]\nproactive_eval = {str(proactive_eval).lower()}\n"
                   f"replied_within_hours = {replied_within_hours}\n")
    log = Path(td) / "decisions.jsonl"
    log.write_text("")
    state = Path(td) / "state.json"
    state.write_text(json.dumps({"_version": 2,
                                 "last_tick": datetime.now(CST).isoformat()}))
    return ChiguoMonitor(str(log), str(state), config_path=str(cfg))


def test_disabled_no_new_key():
    """默认 proactive_eval=False → stats() 不新增 proactive_stats 键。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=False)
        t0 = datetime.now(CST) - timedelta(hours=2)
        _write_entries(Path(td) / "decisions.jsonl", [_send_entry(t0, "lonely_mid")])
        s = mon.stats(days=0)
        assert "proactive_stats" not in s, \
            f"默认关闭恒等，不应新增输出键: {list(s.keys())}"
    print("  OK test_disabled_no_new_key")


def test_enabled_grouped_by_trigger():
    """开启后：按 trigger 分组 sent/replied/reply_rate + overall。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=True)
        t0 = datetime.now(CST) - timedelta(hours=5)
        entries = [
            _send_entry(t0, "lonely_mid"),
            _recv_entry(t0 + timedelta(hours=1)),
            _send_entry(t0 + timedelta(hours=2), "lonely_mid"),
            _recv_entry(t0 + timedelta(hours=3)),
            _send_entry(t0 + timedelta(hours=4), "lonely_low"),
            _recv_entry(t0 + timedelta(hours=5)),
        ]
        _write_entries(Path(td) / "decisions.jsonl", entries)
        s = mon.stats(days=0)
        ps = s["proactive_stats"]
        assert ps["lonely_mid"]["sent"] == 2
        assert ps["lonely_mid"]["replied"] == 2
        assert ps["lonely_mid"]["reply_rate"] == 1.0
        assert ps["lonely_low"]["sent"] == 1 and ps["lonely_low"]["replied"] == 1
        assert ps["overall"]["sent"] == 3 and ps["overall"]["replied"] == 3
        assert ps["overall"]["reply_rate"] == 1.0
    print("  OK test_enabled_grouped_by_trigger")


def test_recv_before_send_not_counted():
    """发送前到达的 recv（非本消息回复）不计入 replied。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=True)
        t0 = datetime.now(CST) - timedelta(hours=4)
        entries = [
            _recv_entry(t0),                 # 发送前到达，不算回复
            _send_entry(t0 + timedelta(hours=1), "lonely_mid"),
            _recv_entry(t0 + timedelta(hours=2)),
            _send_entry(t0 + timedelta(hours=3), "lonely_mid"),
        ]
        _write_entries(Path(td) / "decisions.jsonl", entries)
        s = mon.stats(days=0)
        ps = s["proactive_stats"]
        assert ps["lonely_mid"]["sent"] == 2
        assert ps["lonely_mid"]["replied"] == 1, ps["lonely_mid"]
        assert abs(ps["lonely_mid"]["reply_rate"] - 0.5) < 1e-9
    print("  OK test_recv_before_send_not_counted")


def test_reply_outside_window_not_counted():
    """replied_within_hours 之外的回复不计为已回复。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=True, replied_within_hours=24.0)
        t0 = datetime.now(CST) - timedelta(hours=40)
        entries = [
            _send_entry(t0, "lonely_mid"),
            _recv_entry(t0 + timedelta(hours=30)),  # 30h > 24h 窗口
        ]
        _write_entries(Path(td) / "decisions.jsonl", entries)
        s = mon.stats(days=0)
        ps = s["proactive_stats"]
        assert ps["lonely_mid"]["sent"] == 1
        assert ps["lonely_mid"]["replied"] == 0, ps["lonely_mid"]
        assert ps["lonely_mid"]["reply_rate"] == 0.0
    print("  OK test_reply_outside_window_not_counted")


def test_custom_reply_window():
    """自定义 replied_within_hours 生效（更宽窗口 → 计入）。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=True, replied_within_hours=48.0)
        t0 = datetime.now(CST) - timedelta(hours=40)
        entries = [
            _send_entry(t0, "lonely_mid"),
            _recv_entry(t0 + timedelta(hours=30)),  # 30h ≤ 48h → 计入
        ]
        _write_entries(Path(td) / "decisions.jsonl", entries)
        s = mon.stats(days=0)
        ps = s["proactive_stats"]
        assert ps["lonely_mid"]["replied"] == 1, ps["lonely_mid"]
    print("  OK test_custom_reply_window")


def test_no_sends_no_stats_key():
    """无 send 事件 → proactive_stats 仍输出（空分组 + overall 0）。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=True)
        t0 = datetime.now(CST) - timedelta(hours=2)
        _write_entries(Path(td) / "decisions.jsonl",
                       [_recv_entry(t0), _recv_entry(t0 + timedelta(hours=1))])
        s = mon.stats(days=0)
        ps = s["proactive_stats"]
        assert ps["overall"]["sent"] == 0 and ps["overall"]["replied"] == 0
        assert ps["overall"]["reply_rate"] == 0.0
    print("  OK test_no_sends_no_stats_key")


def test_mixed_triggers_rate_summary():
    """混合触发 + 部分回复 → 各 trigger 独立 reply_rate，overall 汇总。

    消费语义：一条 user-msg 至多算作一条主动消息的回复（命中窗口即消费，
    后续 send 不重复计）。send@+4h 之后无 recv → morning#2 未回复。"""
    with tempfile.TemporaryDirectory() as td:
        mon = _monitor(Path(td), proactive_eval=True)
        t0 = datetime.now(CST) - timedelta(hours=6)
        entries = [
            _send_entry(t0, "morning"),
            _recv_entry(t0 + timedelta(hours=1)),          # 回 send#1
            _send_entry(t0 + timedelta(hours=2), "lonely_high"),
            _recv_entry(t0 + timedelta(hours=3)),          # 回 lonely_high
            _send_entry(t0 + timedelta(hours=4), "morning"),  # 之后无 recv → 未回复
        ]
        _write_entries(Path(td) / "decisions.jsonl", entries)
        s = mon.stats(days=0)
        ps = s["proactive_stats"]
        assert ps["morning"]["sent"] == 2
        assert ps["morning"]["replied"] == 1, ps["morning"]
        assert abs(ps["morning"]["reply_rate"] - 0.5) < 1e-9
        assert ps["lonely_high"]["sent"] == 1
        assert ps["lonely_high"]["replied"] == 1, ps["lonely_high"]
        assert abs(ps["lonely_high"]["reply_rate"] - 1.0) < 1e-9
        assert ps["overall"]["sent"] == 3 and ps["overall"]["replied"] == 2
    print("  OK test_mixed_triggers_rate_summary")
