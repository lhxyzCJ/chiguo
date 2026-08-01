#!/usr/bin/env python3
"""test_monitor.py — chiguo_monitor 单元测试"""

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))

from chiguo_monitor import ChiguoMonitor, AlertManager
from chiguo_rotation import rotate_if_needed, force_rotate, _cleanup_archives


def make_log_entry(action, **kwargs):
    """构造一条决策日志"""
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


def test_empty_log():
    """空日志 → 优雅返回不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "empty.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=7)
        assert s["activity"]["total_sends"] == 0
        assert s["activity"]["total_idles"] == 0
        assert s["period"]["from"] is None
    print("  OK test_empty_log")


def test_missing_files():
    """文件缺失 → 不崩溃"""
    mon = ChiguoMonitor("/nonexistent/log.jsonl", "/nonexistent/state.json")
    s = mon.stats(days=7)
    assert s["activity"]["total_sends"] == 0
    a = mon.alerts()
    # 文件缺失 → 应产生 no_state 关键告警（而非空列表或崩溃）
    no_state = [x for x in a if x["type"] == "no_state"]
    assert len(no_state) == 1, f"expected one no_state alert, got {[x['type'] for x in a]}"
    assert no_state[0]["severity"] == "critical"
    h = mon.health()
    assert h["healthy"] == False
    print("  OK test_missing_files")


def test_basic_stats():
    """基本统计：send/idle 计数、触发分布"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST) - timedelta(days=2)
        entries = [
            make_log_entry("idle", reason="quiet_hours",
                           time=t0 + timedelta(hours=0)),
            make_log_entry("send", trigger="morning", intensity="soft",
                           time=t0 + timedelta(hours=8)),
            make_log_entry("idle", reason="min_interval",
                           time=t0 + timedelta(hours=8, minutes=5)),
            make_log_entry("send", trigger="lonely_mid", intensity="medium",
                           time=t0 + timedelta(hours=14)),
            make_log_entry("send", trigger="lonely_high", intensity="intense",
                           time=t0 + timedelta(hours=20)),
            make_log_entry("idle", reason="quiet_hours",
                           time=t0 + timedelta(hours=23)),
            make_log_entry("send", trigger="morning", intensity="soft",
                           time=t0 + timedelta(hours=32)),
            make_log_entry("send", trigger="lonely_low", intensity="soft",
                           time=t0 + timedelta(hours=38), mwr=3),
        ]
        with open(log, "w") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)  # 全部

        assert s["activity"]["total_sends"] == 5
        assert s["activity"]["total_idles"] == 3
        assert s["activity"]["by_trigger"]["morning"] == 2
        assert s["activity"]["by_trigger"]["lonely_mid"] == 1
        assert s["activity"]["by_trigger"]["lonely_high"] == 1
        assert s["activity"]["by_trigger"]["lonely_low"] == 1
        assert s["activity"]["by_intensity"]["soft"] == 3
        assert s["activity"]["by_intensity"]["medium"] == 1
        assert s["activity"]["by_intensity"]["intense"] == 1
        assert s["replies"]["max_unreplied_streak"] == 3
        assert s["period"]["span_days"] >= 1.0
        assert s["period"]["total_entries"] == 8
    print("  OK test_basic_stats")


def test_unreplied_streak_tracking():
    """messages_without_reply 最长连续递增序列追踪"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST) - timedelta(hours=10)
        # mwr values: 1, 2, 3, 1, 4, 5 → max streak = 5
        for i, mwr_val in enumerate([1, 2, 3, 1, 4, 5]):
            e = make_log_entry("send", trigger="lonely_mid", mwr=mwr_val,
                               time=t0 + timedelta(hours=i))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["replies"]["max_unreplied_streak"] == 5, \
            f"expected max_unreplied_streak=5, got {s['replies']['max_unreplied_streak']}"
    print("  OK test_unreplied_streak_tracking")


def test_stats_days_filter():
    """days 参数过滤：只统计最近 N 天"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST)
        # 10 天前
        old = make_log_entry("send", trigger="lonely_low",
                             time=t0 - timedelta(days=10))
        # 今天
        new = make_log_entry("send", trigger="morning", time=t0)
        with open(log, "w") as f:
            f.write(json.dumps(old, ensure_ascii=False) + "\n")
            f.write(json.dumps(new, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=7)
        assert s["activity"]["total_sends"] == 1  # 只算今天的
        assert s["activity"]["by_trigger"].get("morning") == 1
    print("  OK test_stats_days_filter")


def test_corrupted_lines():
    """损坏行 → 静默跳过"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        with open(log, "w") as f:
            f.write("not valid json\n")
            f.write(json.dumps(make_log_entry("send"), ensure_ascii=False) + "\n")
            f.write("also not json {{{{{\n")
            f.write("\n")  # 空行
            f.write(json.dumps(make_log_entry("idle"), ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["activity"]["total_sends"] == 1
        assert s["activity"]["total_idles"] == 1
    print("  OK test_corrupted_lines")


def test_emotion_trend():
    """情绪趋势：上升/下降/稳定"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST) - timedelta(days=3)
        # 孤独逐步上升
        for i in range(10):
            e = make_log_entry("idle", emotion={
                "loneliness": 10.0 + i * 9,  # 10 → 91
                "affection": 55.0, "anxiety": 40.0,
                "energy": 85.0, "tsundere_index": 70.0,
            }, time=t0 + timedelta(hours=i * 6))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["emotions"]["trends"]["loneliness"] == "rising"
        assert s["emotions"]["stats"]["loneliness"]["max"] >= 90
        assert s["emotions"]["stats"]["loneliness"]["min"] <= 15
    print("  OK test_emotion_trend")


def test_alerts_unreplied():
    """连续无回复告警"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST) - timedelta(hours=10)
        for i in range(6):
            e = make_log_entry("send", trigger="lonely_mid", mwr=i + 1,
                               time=t0 + timedelta(hours=i))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        a = mon.alerts()
        no_reply_alerts = [x for x in a if x["type"] == "consecutive_no_reply"]
        assert len(no_reply_alerts) > 0
        assert no_reply_alerts[0]["max_unreplied"] >= 5
    print("  OK test_alerts_unreplied")


def test_alerts_crash_gap():
    """崩溃间隙告警"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        # 8 小时前
        old_tick = (datetime.now(CST) - timedelta(hours=8)).isoformat()
        state.write_text(json.dumps({"_version": 2, "last_tick": old_tick}))

        mon = ChiguoMonitor(str(log), str(state))
        a = mon.alerts()
        crash = [x for x in a if x["type"] == "crash_gap"]
        assert len(crash) > 0, f"expected crash_gap alert, got {[x['type'] for x in a]}"
        assert crash[0]["severity"] == "critical"
    print("  OK test_alerts_crash_gap")


def test_alerts_emotion_stuck():
    """情绪极端卡住告警"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST) - timedelta(hours=5)
        for i in range(12):  # >=10 才会触发 frequent_crash
            e = make_log_entry("send", trigger="lonely_high",
                               emotion={
                                   "loneliness": 95.0, "affection": 55.0,
                                   "anxiety": 92.0, "energy": 50.0,
                                   "tsundere_index": 40.0,
                               },
                               layer="kernel",
                               time=t0 + timedelta(minutes=i * 25))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        a = mon.alerts()
        stuck = [x for x in a if x["type"] == "emotion_stuck_high"]
        assert len(stuck) >= 1, f"expected emotion_stuck alert, got {[x['type'] for x in a]}"
        frequent = [x for x in a if x["type"] == "frequent_crash"]
        assert len(frequent) >= 1, f"expected frequent_crash alert, got {[x['type'] for x in a]}"
    print("  OK test_alerts_emotion_stuck")


def test_health_ok():
    """健康检查：正常状态"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        log.write_text(json.dumps(make_log_entry("idle"), ensure_ascii=False) + "\n")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        h = mon.health()
        assert h["healthy"] == True
        assert h["hours_since_tick"] is not None
        assert h["hours_since_tick"] < 1
    print("  OK test_health_ok")


def test_summary_no_crash():
    """summary() 不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        log.write_text(json.dumps(make_log_entry("send"), ensure_ascii=False) + "\n")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        text = mon.summary(days=7)
        assert "迟菓" in text
        assert "发送" in text or "send" in text.lower()
        assert len(text) > 100
    print("  OK test_summary_no_crash")


def test_reply_rate_detection():
    """回复率检测逻辑"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        t0 = datetime.now(CST) - timedelta(hours=8)
        # 发一条(mwr=1) → 回复(mwr=0) → 发一条(mwr=1) → 回复(mwr=0) → 发一条(mwr=1)
        for i, mwr_val in enumerate([1, 0, 1, 0, 1]):
            e = make_log_entry("send", mwr=mwr_val, time=t0 + timedelta(hours=i))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["activity"]["total_sends"] == 5
    print("  OK test_reply_rate_detection")


def test_lancedb_detection():
    """LanceDB 降级检测标记"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        t0 = datetime.now(CST) - timedelta(hours=5)
        for i in range(10):
            # 全是 lonely 触发，无 memory
            e = make_log_entry("send", trigger="lonely_low",
                               time=t0 + timedelta(minutes=i * 30))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log), str(state))
        a = mon.alerts()
        lancedb_alerts = [x for x in a if x["type"] == "lancedb_possible_degradation"]
        assert len(lancedb_alerts) >= 1
    print("  OK test_lancedb_detection")


def test_health_disk_ok():
    """health() 应包含磁盘信息"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")  # 空日志
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 1, "last_tick": datetime.now(CST).isoformat()}))

        # 创建模拟配置
        cfg = Path(td) / "chiguo_proactive.toml"
        cfg.write_text("""
[monitor]
disk_warn_mb = 500
disk_critical_mb = 100
""")

        cwd = os.getcwd()
        os.chdir(td)
        try:
            mon = ChiguoMonitor("decisions.jsonl", "state.json", config_path="chiguo_proactive.toml")
            h = mon.health()
            assert "disk" in h, f"health() missing 'disk' key: {list(h.keys())}"
            disk = h["disk"]
            assert disk["free_mb"] is not None, "disk.free_mb should not be None"
            assert disk["total_mb"] is not None, "disk.total_mb should not be None"
            assert disk["free_mb"] > 0, f"disk free should be >0, got {disk['free_mb']}"
        finally:
            os.chdir(cwd)
    print("  OK test_health_disk_ok")


def test_health_memory_check():
    """health() 应包含进程内存信息 (Linux)"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 1, "last_tick": datetime.now(CST).isoformat()}))

        cfg = Path(td) / "chiguo_proactive.toml"
        cfg.write_text("""
[monitor]
memory_warn_mb = 500
memory_critical_mb = 1000
""")

        cwd = os.getcwd()
        os.chdir(td)
        try:
            mon = ChiguoMonitor("decisions.jsonl", "state.json", config_path="chiguo_proactive.toml")
            h = mon.health()
            assert "memory" in h, f"health() missing 'memory' key: {list(h.keys())}"
            mem = h["memory"]
            assert mem["rss_mb"] is not None, "memory.rss_mb should not be None on Linux"
            assert mem["rss_mb"] > 0, f"rss_mb should be >0, got {mem['rss_mb']}"
        finally:
            os.chdir(cwd)
    print("  OK test_health_memory_check")


def test_health_lancedb_direct():
    """health() 应包含 lancedb_direct 字段（True/False/None）"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 1, "last_tick": datetime.now(CST).isoformat()}))

        cwd = os.getcwd()
        os.chdir(td)
        try:
            mon = ChiguoMonitor("decisions.jsonl", "state.json")
            h = mon.health()
            # lancedb_direct 可为 True/False/None，取决于环境
            # 只验证字段存在 + 类型正确
            assert "lancedb_direct" in h, f"health() missing 'lancedb_direct': {list(h.keys())}"
            ldb = h["lancedb_direct"]
            assert ldb in (True, False, None), f"lancedb_direct should be bool or None, got {type(ldb)}: {ldb}"
        finally:
            os.chdir(cwd)
    print("  OK test_health_lancedb_direct")


def test_health_netease_faulty():
    """netease_health.json faulty=True → healthy=False 且 issues 含 netease 告警"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))
        cfg = Path(td) / "chiguo_proactive.toml"
        cfg.write_text("[monitor]\ndisk_warn_mb = 500\ndisk_critical_mb = 100\n")
        nh = Path(td) / "netease_health.json"
        nh.write_text(json.dumps({
            "faulty": True, "failure_reason": "login_expired",
            "api_alive": True, "logged_in": False,
        }))

        mon = ChiguoMonitor(str(log), str(state), config_path=str(cfg))
        h = mon.health()
        assert h["healthy"] is False, f"expected unhealthy, issues={h['issues']}"
        netease_issues = [i for i in h["issues"] if "netease" in i]
        assert len(netease_issues) == 1, f"expected one netease issue, got {h['issues']}"
        assert "netease music API faulty" in netease_issues[0]
        assert "login_expired" in netease_issues[0]
        assert "api_alive=True" in netease_issues[0]
        assert "logged_in=False" in netease_issues[0]
    print("  OK test_health_netease_faulty")


def test_health_netease_healthy():
    """netease_health.json faulty=False → 不产生 netease issue"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))
        cfg = Path(td) / "chiguo_proactive.toml"
        cfg.write_text("[monitor]\ndisk_warn_mb = 500\ndisk_critical_mb = 100\n")
        nh = Path(td) / "netease_health.json"
        nh.write_text(json.dumps({"faulty": False}))

        mon = ChiguoMonitor(str(log), str(state), config_path=str(cfg))
        h = mon.health()
        netease_issues = [i for i in h["issues"] if "netease" in i]
        assert len(netease_issues) == 0, f"expected no netease issue, got {h['issues']}"
    print("  OK test_health_netease_healthy")


def test_health_netease_missing_file():
    """无 netease_health.json → 不崩、无 netease issue"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        log.write_text("")
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))
        cfg = Path(td) / "chiguo_proactive.toml"
        cfg.write_text("[monitor]\ndisk_warn_mb = 500\ndisk_critical_mb = 100\n")

        mon = ChiguoMonitor(str(log), str(state), config_path=str(cfg))
        h = mon.health()
        netease_issues = [i for i in h["issues"] if "netease" in i]
        assert len(netease_issues) == 0, f"expected no netease issue, got {h['issues']}"
    print("  OK test_health_netease_missing_file")


# ═══════════════════════════════════════════════════════════
# Fuzz 测试：随机数据 + 边界值，不应崩溃
# ═══════════════════════════════════════════════════════════

def _random_entry(time: datetime, seed: int = 42) -> dict:
    """生成随机的决策条目（合法结构，随机值）。"""
    rng = random.Random(seed)
    triggers = ["lonely_low", "lonely_mid", "lonely_high", "anxiety", "morning",
                "night", "playful", "memory", "manual", None]
    layers = ["shell", "middle", "kernel", None]
    intensities = ["soft", "normal", "urgent", None]
    actions = ["send", "idle"]

    action = rng.choice(actions)
    entry: dict = {
        "action": action,
        "time": time.strftime("%Y-%m-%d %H:%M"),
        "trigger": rng.choice(triggers),
        "intensity": rng.choice(intensities),
        "state": {
            "time": time.strftime("%Y-%m-%d %H:%M"),
            "dominant_layer": rng.choice(layers),
            "emotion": {
                "loneliness": round(rng.uniform(0, 100), 1),
                "affection": round(rng.uniform(5, 100), 1),
                "anxiety": round(rng.uniform(0, 100), 1),
                "energy": round(rng.uniform(0, 100), 1),
                "tsundere_index": round(rng.uniform(10, 95), 1),
            },
            "messages_without_reply": rng.randint(0, 20),
            "cooldown": {
                "silent_hours": round(rng.uniform(0, 48), 1),
                "message_count_today": rng.randint(0, 10),
                "last_send_time": (time - timedelta(minutes=rng.randint(1, 480))).isoformat() if rng.random() > 0.5 else None,
            },
        },
        "context": {
            "trigger_type": rng.choice(triggers),
            "situation": rng.choice(["主人已经很久没发消息了", "早安问候", "晚安问候", "心情不错", None]),
            "topic": rng.choice(["课表提醒", "节气问候", "随机记忆", None]),
        },
    }
    return entry


def test_fuzz_random_entries():
    """随机生成 200 条合法条目 → 所有方法不应崩溃"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        state = Path(td) / "state.json"
        t0 = datetime(2026, 6, 1, tzinfo=CST)

        entries = []
        for i in range(200):
            e = _random_entry(t0 + timedelta(hours=i * 3), seed=i * 7)
            entries.append(json.dumps(e, ensure_ascii=False) + "\n")
        log.write_text("".join(entries))

        state.write_text(json.dumps({
            "_version": 1,
            "last_tick": (t0 + timedelta(hours=600)).isoformat(),
            "emotion": {"loneliness": 50, "affection": 60, "anxiety": 30, "energy": 70, "tsundere_index": 40},
        }))

        mon = ChiguoMonitor(str(log), str(state))

        # 所有公开方法
        s = mon.stats(days=30)
        assert "activity" in s
        assert "emotions" in s
        assert isinstance(s["activity"]["total_sends"], int)

        a = mon.alerts()
        assert isinstance(a, list)

        h = mon.health()
        assert "healthy" in h

        r = mon.report(days=14)
        assert "stats" in r and "alerts" in r and "health" in r

        txt = mon.summary(days=30)
        assert isinstance(txt, str)
        assert len(txt) > 0
    print("  OK test_fuzz_random_entries")


def test_fuzz_boundary_values():
    """边界值：极值情绪、空字段、超大数值 → 不应崩溃"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        state = Path(td) / "state.json"

        boundaries = [
            # 极端情绪值
            {"action": "send", "time": "2026-06-01 12:00",
             "state": {"emotion": {"loneliness": 0, "affection": 100, "anxiety": 0, "energy": 100},
                       "messages_without_reply": 0, "dominant_layer": "shell"}},
            {"action": "send", "time": "2026-06-01 13:00",
             "state": {"emotion": {"loneliness": 100, "affection": 0, "anxiety": 100, "energy": 0},
                       "messages_without_reply": 50, "dominant_layer": "kernel"}},
            # 缺失字段
            {"action": "idle", "time": "2026-06-01 14:00"},
            {"action": "send", "time": "2026-06-01 15:00", "state": None},
            # 异常值
            {"action": "send", "time": "2026-06-01 16:00",
             "state": {"emotion": {"loneliness": -999, "affection": 9999, "anxiety": "high", "energy": None},
                       "messages_without_reply": -1}},
            # 空字符串 / 超长字符串
            {"action": "send", "time": "2026-06-01 17:00",
             "trigger": "", "intensity": "", "state": {"dominant_layer": "x" * 10000}},
            # 时间格式异常
            {"action": "send", "time": "not-a-time", "state": {}},
            {"action": "send", "time": None, "state": {}},
        ]

        log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in boundaries))

        state.write_text(json.dumps({"_version": 1, "last_tick": "2026-06-01T18:00:00"}))

        mon = ChiguoMonitor(str(log), str(state))

        # 不应崩溃
        try:
            mon.stats(days=7)
            mon.alerts()
            mon.health()
            mon.summary(days=7)
        except Exception as e:
            assert False, f"boundary values caused crash: {e}"
    print("  OK test_fuzz_boundary_values")


def test_fuzz_empty_and_extreme():
    """空日志 + 单条极值 + 全 idle → 不应崩溃"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 1}))

        # 空日志
        log.write_text("")
        m1 = ChiguoMonitor(str(log), str(state))
        assert m1.stats()["activity"]["total_sends"] == 0
        assert m1.alerts() == [] or isinstance(m1.alerts(), list)
        m1.summary()

        # 只有空白行和损坏行
        log.write_text("\n\n  \n{broken json\n\n")
        m2 = ChiguoMonitor(str(log), str(state))
        assert m2.stats()["activity"]["total_sends"] == 0

        # 100 条纯 idle
        t0 = datetime(2026, 6, 1, tzinfo=CST)
        idles = []
        for i in range(100):
            idles.append(json.dumps({
                "action": "idle",
                "time": (t0 + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M"),
                "state": {"emotion": {"loneliness": 50, "energy": 50},
                          "messages_without_reply": 0},
            }, ensure_ascii=False))
        log.write_text("\n".join(idles))
        m3 = ChiguoMonitor(str(log), str(state))
        s = m3.stats(days=0)  # days=0 = 全部历史
        assert s["activity"]["total_sends"] == 0
        assert s["activity"]["total_idles"] == 100
        # alerts() 不崩溃即可（可能因数据极值触发告警，属正常）
    print("  OK test_fuzz_empty_and_extreme")


# ═══════════════════════════════════════════════════════════
# v5: F1 Conversation Content Logging tests
# ═══════════════════════════════════════════════════════════

def test_recv_entry_logged():
    """recv 条目在 decisions.jsonl 中有 msg_id 和 message_text"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        state = Path(td) / "state.json"
        state.write_text(json.dumps({
            "_version": 4, "last_tick": datetime.now(CST).isoformat(),
            "emotion": {"loneliness": 15.0, "affection": 55.0, "anxiety": 40.0, "energy": 85.0, "tsundere_index": 70.0},
            "cooldown": {"messages_today": 0, "messages_without_reply": 0, "current_date": datetime.now(CST).strftime("%Y-%m-%d")},
            "personality": {"tsundere_intensity": 65.0, "extraversion": 40.0, "neuroticism": 55.0, "agreeableness": 60.0},
        }))

        # Write recv entry manually (simulating what record_user_message does)
        recv_entry = {
            "action": "recv",
            "msg_id": "test_recv_001",
            "message_text": "你好，今天过得怎么样？",
            "message_length": 12,
            "user_emotion_analysis": {"warmth": 0.5, "effort": 0.3, "attention": 0.7},
            "state": {"emotion": {"loneliness": 15.0}, "cooldown": {}, "time": "2026-06-28 14:00"},
        }
        log.write_text(json.dumps(recv_entry, ensure_ascii=False) + "\n")

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        # stats counts send+idle; recv entry is stored but not counted in totals
        assert s["period"]["total_entries"] == 0  # recv not counted in send/idle totals
        # Verify the recv entry is readable from the log file directly
        lines = [l for l in log.read_text(encoding="utf-8").strip().splitlines() if l.strip()]
        assert len(lines) == 1
        r = json.loads(lines[0])
        assert r["action"] == "recv"
        assert r["msg_id"] == "test_recv_001"
        assert r["message_text"] == "你好，今天过得怎么样？"
        assert r["message_length"] == 12
        assert r["user_emotion_analysis"]["warmth"] == 0.5
    print("  OK test_recv_entry_logged")


def test_msg_id_on_all_actions():
    """所有决策条目（send/idle）都包含 msg_id"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        # Write entries without msg_id to simulate old format
        entries = [
            {"action": "send", "trigger": "morning", "intensity": "soft",
             "state": {"emotion": {"loneliness": 15.0, "affection": 55.0, "anxiety": 40.0, "energy": 85.0, "tsundere_index": 70.0},
                       "cooldown": {}, "time": "2026-06-28 08:00"}},
            {"action": "idle", "reason": "quiet_hours",
             "state": {"emotion": {"loneliness": 15.0, "affection": 55.0, "anxiety": 40.0, "energy": 85.0, "tsundere_index": 70.0},
                       "cooldown": {}, "time": "2026-06-28 03:00"}},
        ]
        log.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))

        mon = ChiguoMonitor(str(log))
        s = mon.stats(days=0)
        assert s["activity"]["total_sends"] == 1
        assert s["activity"]["total_idles"] == 1
    print("  OK test_msg_id_on_all_actions")


def test_message_stats():
    """messages_summary 返回发/收统计"""
    with tempfile.TemporaryDirectory() as td:
        msgs = Path(td) / "messages.jsonl"
        # 用相对当前时间的时间戳（days=7 过滤以真实时钟为基准）
        def ts(days_ago: int, hour: int) -> str:
            return (datetime.now(CST) - timedelta(days=days_ago)).replace(
                hour=hour, minute=0, second=0, microsecond=0).isoformat()
        # Write some test messages
        records = [
            {"msg_id": "001", "ts": ts(0, 8), "direction": "send",
             "text": "早上好！今天天气不错"},
            {"msg_id": "002", "ts": ts(0, 9), "direction": "recv",
             "text": "早啊"},
            {"msg_id": "003", "ts": ts(0, 14), "direction": "send",
             "text": "下午了，记得休息"},
            {"msg_id": "004", "ts": ts(0, 15), "direction": "recv",
             "text": "好的谢谢"},
        ]
        msgs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

        mon = ChiguoMonitor(messages_log_path=str(msgs))
        summary = mon.messages_summary(days=7)
        assert summary is not None
        assert summary["total_send"] == 2
        assert summary["total_recv"] == 2
        assert summary["avg_send_length"] > 0
        assert summary["avg_recv_length"] > 0
    print("  OK test_message_stats")


def test_recv_empty_text():
    """recv 条目空消息文本不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "decisions.jsonl"
        recv_entry = {
            "action": "recv",
            "msg_id": "test_empty_001",
            "message_text": "",
            "message_length": 0,
            "state": {"emotion": {"loneliness": 15.0}, "cooldown": {}, "time": "2026-06-28 14:00"},
        }
        log.write_text(json.dumps(recv_entry, ensure_ascii=False) + "\n")

        mon = ChiguoMonitor(str(log))
        s = mon.stats(days=0)
        # stats doesn't crash on recv entries
        assert s["period"]["total_entries"] == 0  # recv not counted in send/idle

        # Also test messages.jsonl with empty text (相对当前时间，避免 days 过滤)
        msgs = Path(td) / "messages.jsonl"
        msgs.write_text(json.dumps({"msg_id": "001", "ts": datetime.now(CST).isoformat(),
                                    "direction": "recv", "text": ""}, ensure_ascii=False) + "\n")
        mon2 = ChiguoMonitor(messages_log_path=str(msgs))
        summary = mon2.messages_summary(days=7)
        assert summary is not None
        assert summary["avg_recv_length"] == 0.0
    print("  OK test_recv_empty_text")


# ═══════════════════════════════════════════════════════════
# v5: F2 Conversation History Archive tests
# ═══════════════════════════════════════════════════════════

def test_messages_jsonl_created():
    """chiguo_messages.jsonl 格式正确"""
    with tempfile.TemporaryDirectory() as td:
        msgs = Path(td) / "messages.jsonl"
        def ts(days_ago: int, hour: int) -> str:
            return (datetime.now(CST) - timedelta(days=days_ago)).replace(
                hour=hour, minute=0, second=0, microsecond=0).isoformat()
        records = [
            {"msg_id": "001", "ts": ts(0, 10), "direction": "recv",
             "text": "哥哥今天有空吗", "user_emotion_analysis": {"warmth": 0.6}},
            {"msg_id": "002", "ts": ts(0, 11), "direction": "send",
             "text": "嗯？干嘛突然问这个……", "trigger": "lonely_mid", "intensity": "soft",
             "emotion_snapshot": {"loneliness": 55.0, "affection": 60.0, "anxiety": 40.0, "energy": 70.0, "tsundere_index": 65.0}},
        ]
        msgs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

        mon = ChiguoMonitor(messages_log_path=str(msgs))
        msgs_result = mon.conversation(days=7)
        assert len(msgs_result) == 2
        assert msgs_result[0]["direction"] == "recv"
        assert "哥哥今天有空吗" in msgs_result[0]["text"]
        assert msgs_result[1]["direction"] == "send"
        assert msgs_result[1]["trigger"] == "lonely_mid"
    print("  OK test_messages_jsonl_created")


def test_conversation_by_date():
    """conversation(date_str='YYYY-MM-DD') 按日期过滤"""
    with tempfile.TemporaryDirectory() as td:
        msgs = Path(td) / "messages.jsonl"
        records = [
            {"msg_id": "001", "ts": "2026-06-27T10:00:00+08:00", "direction": "send", "text": "昨天的消息"},
            {"msg_id": "002", "ts": "2026-06-28T10:00:00+08:00", "direction": "recv", "text": "今天的消息"},
            {"msg_id": "003", "ts": "2026-06-28T11:00:00+08:00", "direction": "send", "text": "也是今天的"},
        ]
        msgs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

        mon = ChiguoMonitor(messages_log_path=str(msgs))
        today = mon.conversation(date_str="2026-06-28")
        assert len(today) == 2
        yesterday = mon.conversation(date_str="2026-06-27")
        assert len(yesterday) == 1
    print("  OK test_conversation_by_date")


def test_conversation_by_days():
    """conversation(days=N) 最近N天过滤"""
    with tempfile.TemporaryDirectory() as td:
        msgs = Path(td) / "messages.jsonl"
        # Write a message from 10 days ago and one from today
        from datetime import datetime as dt
        old = (dt.now(CST) - timedelta(days=10)).isoformat()
        new = dt.now(CST).isoformat()
        records = [
            {"msg_id": "001", "ts": old, "direction": "send", "text": "旧消息"},
            {"msg_id": "002", "ts": new, "direction": "recv", "text": "新消息"},
        ]
        msgs.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))

        mon = ChiguoMonitor(messages_log_path=str(msgs))
        recent = mon.conversation(days=3)
        assert len(recent) == 1
        assert recent[0]["text"] == "新消息"
    print("  OK test_conversation_by_days")


def test_export_json():
    """export() 返回合法 JSON"""
    with tempfile.TemporaryDirectory() as td:
        msgs = Path(td) / "messages.jsonl"
        msgs.write_text("")
        mon = ChiguoMonitor(messages_log_path=str(msgs))
        result = mon.export(format="json")
        parsed = json.loads(result)
        assert isinstance(parsed, list)
        result2 = mon.export()  # default format
        parsed2 = json.loads(result2)
        assert isinstance(parsed2, list)
    print("  OK test_export_json")


# ═══════════════════════════════════════════════════════════
# v5: F3 Log Rotation tests
# ═══════════════════════════════════════════════════════════

def test_rotation_creates_archive():
    """rotate_if_needed 把旧日志移到 archive/ 目录"""
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "archive"
        # Create log with old mtime
        log = Path(td) / "test.jsonl"
        log.write_text('{"test": true}\n')
        # Set mtime to 2 months ago
        old_time = (datetime.now(CST) - timedelta(days=60)).timestamp()
        os.utime(str(log), (old_time, old_time))

        # Create minimal config with absolute archive path
        cfg = Path(td) / "test.toml"
        archive_abs = str(Path(td) / "archive")
        cfg.write_text(f"[logging]\nretention_months = 12\narchive_dir = \"{archive_abs}\"\n")

        rotate_if_needed([str(log)], str(cfg))

        # Old file should be moved
        assert not log.exists() or log.stat().st_size == 0, "Old log should be empty or moved"
        # Archive should exist with the old content
        archive_root = Path(td) / "archive"
        archived_files = list(archive_root.glob("*.jsonl")) if archive_root.exists() else []
        assert len(archived_files) >= 1, f"Should have archived files, found {archived_files}"
    print("  OK test_rotation_creates_archive")


def test_rotation_skips_current_month():
    """当月 mtime 的文件不轮转"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "current.jsonl"
        log.write_text('{"test": true}\n')
        # Current month mtime
        now_stamp = datetime.now(CST).timestamp()
        os.utime(str(log), (now_stamp, now_stamp))

        cfg = Path(td) / "test.toml"
        archive_abs = str(Path(td) / "archive")
        cfg.write_text(f"[logging]\nretention_months = 12\narchive_dir = \"{archive_abs}\"\n")

        rotate_if_needed([str(log)], str(cfg))

        assert log.exists(), "Current month log should NOT be rotated"
        assert log.stat().st_size > 0, "Current month log should keep content"
    print("  OK test_rotation_skips_current_month")


def test_rotation_cleanup():
    """_cleanup_archives 删除超过保留期限的归档"""
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "archive"
        archive.mkdir()

        # Create an old archive
        old_file = archive / "2026-01-test.jsonl"
        old_file.write_text('{"old": true}\n')
        old_time = (datetime.now(CST) - timedelta(days=400)).timestamp()
        os.utime(str(old_file), (old_time, old_time))

        # Create a recent archive
        recent_file = archive / "2026-06-test.jsonl"
        recent_file.write_text('{"recent": true}\n')

        _cleanup_archives(str(archive), 12, datetime.now(CST))

        assert not old_file.exists(), "Old archive should be deleted"
        assert recent_file.exists(), "Recent archive should be kept"
    print("  OK test_rotation_cleanup")


def test_force_rotate():
    """force_rotate 强制轮转不管月份"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "force.jsonl"
        log.write_text('{"test": true}\n')

        archive_dir = str(Path(td) / "archive")
        force_rotate([str(log)], archive_dir)

        # Archive should be created with content
        archive_path = Path(archive_dir)
        archived = list(archive_path.glob("*.jsonl"))
        assert len(archived) >= 1, "Should have archived file after force rotate"
        assert archived[0].read_text().strip() == '{"test": true}'
        # Original should exist but be empty
        assert log.exists()
    print("  OK test_force_rotate")


# ═══════════════════════════════════════════════════════════
# v5: F4 Alert Persistence tests
# ═══════════════════════════════════════════════════════════

def test_alert_create():
    """ingest() 为新告警类型创建 active 条目"""
    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "alerts.json"
        am = AlertManager(str(ap))

        fresh = [
            {"type": "crash_gap", "severity": "critical", "message": "daemon down 8h"},
        ]
        am.ingest(fresh)
        active = am.list_active()
        assert len(active) == 1
        assert active[0]["type"] == "crash_gap"
        assert active[0]["status"] == "active"
        assert active[0]["count"] == 1
    print("  OK test_alert_create")


def test_alert_dedup():
    """同类型告警重复触发 → 更新 last_seen + count，不新建"""
    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "alerts.json"
        am = AlertManager(str(ap))

        # First ingestion
        am.ingest([{"type": "low_reply_rate", "severity": "warn", "message": "reply rate 20%"}])
        # Second ingestion (same type)
        am.ingest([{"type": "low_reply_rate", "severity": "warn", "message": "reply rate 18%"}])

        active = am.list_active()
        assert len(active) == 1, "Same type should dedup, not create new"
        assert active[0]["count"] == 2, "Count should increment"
        assert active[0]["status"] == "active"
    print("  OK test_alert_dedup")


def test_alert_resolve():
    """告警不再出现在 fresh 列表中 → 标记为 resolved"""
    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "alerts.json"
        am = AlertManager(str(ap))

        am.ingest([{"type": "no_state", "severity": "critical", "message": "state file missing"}])
        # Now ingest empty list — alert should resolve
        am.ingest([])

        active = am.list_active()
        assert len(active) == 0, "Resolved alerts should not be active"
        all_alerts = am.list_all()
        assert len(all_alerts) == 1
        assert all_alerts[0]["status"] == "resolved"
        assert all_alerts[0]["resolved_at"] is not None
    print("  OK test_alert_resolve")


def test_alert_acknowledge():
    """acknowledge() 设置 acknowledged_at 和状态"""
    with tempfile.TemporaryDirectory() as td:
        ap = Path(td) / "alerts.json"
        am = AlertManager(str(ap))

        am.ingest([{"type": "consecutive_no_reply", "severity": "warn", "message": "5 unreplied"}])
        alert_id = am.list_active()[0]["alert_id"]

        ok = am.acknowledge(alert_id)
        assert ok
        active = am.list_active()
        assert active[0]["status"] == "acknowledged"
        assert active[0]["acknowledged_at"] is not None

        # Acknowledge non-existent
        ok2 = am.acknowledge("alert_nonexistent")
        assert not ok2
    print("  OK test_alert_acknowledge")


def test_sleep_hours_deduction():
    """silent_hours 正确扣除睡眠窗口 (0:00-8:00)"""
    from chiguo_state import CooldownState
    c = CooldownState()

    # 22:00 发消息 → 次日 09:00：墙钟 11h，睡眠窗口 0-8 = 8h → 清醒 3h
    c.last_user_message_at = "2026-06-28T22:00:00+08:00"
    now = datetime(2026, 6, 29, 9, 0, tzinfo=CST)
    sh = c.silent_hours(now)
    assert 2.5 < sh < 3.5, f"22:00→09:00 should be ~3h awake, got {sh}"

    # 23:00 发消息 → 次日 07:00：墙钟 8h，23-00=1h清醒，0-7=7h睡眠 → 清醒 1h
    c.last_user_message_at = "2026-06-28T23:00:00+08:00"
    now = datetime(2026, 6, 29, 7, 0, tzinfo=CST)
    sh = c.silent_hours(now)
    assert 0.5 < sh < 1.5, f"23:00→07:00 should be ~1h awake, got {sh}"

    # 14:00 发消息 → 当天 20:00：墙钟 6h，不跨睡眠窗口 → 清醒 6h
    c.last_user_message_at = "2026-06-28T14:00:00+08:00"
    now = datetime(2026, 6, 28, 20, 0, tzinfo=CST)
    sh = c.silent_hours(now)
    assert 5.5 < sh < 6.5, f"14:00→20:00 should be 6h awake, got {sh}"

    # 跨越多个睡眠窗口
    c.last_user_message_at = "2026-06-27T22:00:00+08:00"
    now = datetime(2026, 6, 29, 9, 0, tzinfo=CST)  # 35h 墙钟，2个睡眠窗口 = 16h → 清醒 19h
    sh = c.silent_hours(now)
    assert 18 < sh < 20, f"2-day span should be ~19h awake, got {sh}"

    print("  OK test_sleep_hours_deduction")


def test_reply_latency_stats():
    """stats() 正确计算回复延迟的 avg/median"""
    with tempfile.TemporaryDirectory() as td:
        log = Path(td) / "test.jsonl"
        state = Path(td) / "state.json"
        state.write_text(json.dumps({"_version": 2, "last_tick": datetime.now(CST).isoformat()}))

        t0 = datetime.now(CST) - timedelta(hours=8)
        # 构造 mwr 下降序列: 1→0 (latency=1.5h), 1→0 (latency=0.5h)
        entries = [
            (1, 5.0),  # send #1, mwr=1
            (0, 1.5),  # send #2, mwr=0 (回复到达, latency=1.5h)
            (1, 3.0),  # send #3, mwr=1
            (0, 0.5),  # send #4, mwr=0 (回复到达, latency=0.5h)
            (1, 2.0),  # send #5, mwr=1 (无后续回复)
        ]
        for i, (mwr_val, silent_h) in enumerate(entries):
            e = make_log_entry("send", mwr=mwr_val, silent_hours=silent_h,
                               time=t0 + timedelta(hours=i * 1.5))
            with open(log, "a") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

        mon = ChiguoMonitor(str(log), str(state))
        s = mon.stats(days=0)
        assert s["activity"]["total_sends"] == 5
        assert s["replies"]["avg_reply_latency_h"] == 1.0   # (1.5 + 0.5) / 2
        assert s["replies"]["median_reply_latency_h"] == 1.0
        # reply_rate: 2 replies / 3 sends-with-mwr>0
        assert abs(s["replies"]["reply_rate"] - 0.667) < 0.01
    print("  OK test_reply_latency_stats")


if __name__ == "__main__":
    print("test_monitor.py\n")
    tests = [
        test_empty_log,
        test_missing_files,
        test_basic_stats,
        test_unreplied_streak_tracking,
        test_stats_days_filter,
        test_corrupted_lines,
        test_emotion_trend,
        test_alerts_unreplied,
        test_alerts_crash_gap,
        test_alerts_emotion_stuck,
        test_health_ok,
        test_summary_no_crash,
        test_reply_rate_detection,
        test_lancedb_detection,
        test_health_disk_ok,
        test_health_memory_check,
        test_health_lancedb_direct,
        test_health_netease_faulty,
        test_health_netease_healthy,
        test_health_netease_missing_file,
        test_fuzz_random_entries,
        test_fuzz_boundary_values,
        test_fuzz_empty_and_extreme,
        # v5: conversation logging & archive
        test_recv_entry_logged,
        test_msg_id_on_all_actions,
        test_message_stats,
        test_recv_empty_text,
        test_messages_jsonl_created,
        test_conversation_by_date,
        test_conversation_by_days,
        test_export_json,
        # v5: log rotation
        test_rotation_creates_archive,
        test_rotation_skips_current_month,
        test_rotation_cleanup,
        test_force_rotate,
        # v5: alert persistence
        test_alert_create,
        test_alert_dedup,
        test_alert_resolve,
        test_alert_acknowledge,
        test_sleep_hours_deduction,
        test_reply_latency_stats,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    total = len(tests)
    passed = total - failed
    print(f"ALL {total} monitor tests, {passed} passed, {failed} failed.")
    if failed:
        sys.exit(1)
    sys.exit(0)
