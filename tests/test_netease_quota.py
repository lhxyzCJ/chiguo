#!/usr/bin/env python3
"""test_netease_quota.py — T16 Netease 硬度：peek幂等 + quota 日切 + health 8级降级链

TDD 契约表（RED→GREEN）：
- peek 3次 quota 不变、consume 1次 -1
- daily 午夜惰性归零（quota_music_day != today→reset）
- faulty 时段门禁前置（in_class/in_quiet 恒 None，即使 faulty）
- 增强幂等：peek 同步恢复健康但不 increment used，consume 选中后 _sync_success；both sources fail → refresh_health
- 健康 8级降级链：api_alive/logged_in/failure_reason 8组合 + 301→login_expired
- 保留 <base_dir>/netease/ 锚定、bridge 可注入 fake、data_dir.mkdir、health 0600、source_weights cfg_float clamp
"""
import json
import os
import random
import stat
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 31, 22, 30, 0, tzinfo=CST)
NEXT_DAY = datetime(2026, 8, 1, 9, 0, 0, tzinfo=CST)
NOON_NEXT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=CST)

from netease.service import NeteaseService, HEALTH_SCHEMA_KEYS


def _songs(n=3):
    return [{"id": i, "name": f"歌{i}", "artists": "歌手"} for i in range(1, n + 1)]


def _plays():
    return [{"playTime": 1722441600000, "name": "夜曲", "artist": "周杰伦"}]


class _FakeBridge:
    def __init__(self, daily=None, recent=None, health=None):
        self._daily = daily
        self._recent = recent
        self._health = health
        self.calls = {"daily": 0, "recent": 0, "health": 0}

    def fetch_daily_songs(self, *a, **k):
        self.calls["daily"] += 1
        return self._daily(*a, **k) if self._daily else None

    def fetch_recent_play(self, *a, **k):
        self.calls["recent"] += 1
        return self._recent(*a, **k) if self._recent else None

    def check_health(self):
        self.calls["health"] += 1
        if self._health:
            return self._health()
        return {"api_alive": True, "logged_in": True, "profile": {}}


def _make(td, quota=2, fault_quota=1, weights=None, enabled=True, **fakes):
    cfg = {
        "netease": {"retry_count": 0, "retry_backoff_seconds": 0.0, "reprobe_minutes": 30.0, "enabled": enabled},
        "topic_picker": {"netease_daily_quota": quota, "netease_fault_daily_quota": fault_quota},
    }
    if weights is not None:
        cfg["topic_picker"]["netease_source_weights"] = weights
    return NeteaseService(cfg, td, bridge=_FakeBridge(**fakes))


# ── 1. peek 幂等：3次不消费 ─────────────────────────────

def test_peek_idempotent_three_times_no_consume():
    """peek 3次 quota 不变（幂等）；consume 1次 -1"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, quota=2, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        t1 = svc.peek_music_topic(NOW)
        assert t1 is not None and t1["type"] == "netease_music"
        assert svc._health["quota_music_used"] == 0, "peek 1 不消费"
        t2 = svc.peek_music_topic(NOW)
        assert t2 is not None
        assert svc._health["quota_music_used"] == 0, "peek 2 不消费"
        t3 = svc.peek_music_topic(NOW)
        assert t3 is not None
        assert svc._health["quota_music_used"] == 0, "peek 3 不消费"
        # consume 1次 → -1
        svc.consume_music_topic(NOW)
        assert svc._health["quota_music_used"] == 1
        # 剩余1
        assert svc._music_quota_left(NOW) == 1
        svc.consume_music_topic(NOW)
        assert svc._health["quota_music_used"] == 2
        assert svc.peek_music_topic(NOW) is None, "quota耗尽 peek None"
        assert svc._health["quota_music_used"] == 2, "超额不递增"
    print("  OK test_peek_idempotent_three_times_no_consume")


def test_consume_only_once_per_pick():
    """consume 仅1次 -1，重复 peek 不累计"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, quota=5, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        for _ in range(5):
            svc.peek_music_topic(NOW)
        assert svc._health["quota_music_used"] == 0
        svc.consume_music_topic(NOW)
        assert svc._health["quota_music_used"] == 1
        # 再次 peek 3次仍不变
        for _ in range(3):
            svc.peek_music_topic(NOW)
        assert svc._health["quota_music_used"] == 1
    print("  OK test_consume_only_once_per_pick")


def test_peek_sync_recovers_health_without_consume():
    """增强契约：peek 成功的 _sync_success 恢复 faulty，但不 increment used"""
    with tempfile.TemporaryDirectory() as td:
        # 先置 faulty
        svc = _make(td, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays(),
                    health=lambda: {"api_alive": False, "logged_in": False})
        svc.refresh_health(NOW)
        assert svc.health()["faulty"] is True
        assert svc.health()["failure_reason"] == "unreachable"
        # 重置 bridge 为可用，peek 触发 _pick_and_fetch → 成功 → _sync_success
        svc.bridge._health = lambda: {"api_alive": True, "logged_in": True}
        # 手动清除 faulty 的 last_check 过期以触发直接 pick 而非故障分支？
        # 实际 peek faulty分支会先 reprobe（30min 内不探），所以先把 last_check 设旧
        svc._health["last_check"] = (NOW - timedelta(minutes=40)).isoformat()
        svc.bridge._daily = lambda *a, **k: _songs()
        svc.bridge._recent = lambda *a, **k: _plays()
        t = svc.peek_music_topic(NOW)
        # faulty已恢复 → 应产 music 而非 fault
        assert t is not None and t["type"] == "netease_music", t
        assert svc.health()["faulty"] is False, "peek 成功应恢复 health"
        assert svc._health["quota_music_used"] == 0, "恢复不消费"
        assert svc._health["quota_fault_used"] == 0
    print("  OK test_peek_sync_recovers_health_without_consume")


def test_consume_after_pick_syncs_success():
    """consume 选中后 _sync_success：_pick_and_fetch consume=True 路径同样恢复"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays(),
                    health=lambda: {"api_alive": False, "logged_in": False})
        svc.refresh_health(NOW)
        assert svc.health()["faulty"] is True
        # 直接调 _pick_and_fetch consume=True
        t = svc._pick_and_fetch(NOW, consume=True)
        assert t is not None
        assert svc.health()["faulty"] is False
        assert svc._health["quota_music_used"] == 1, "consume=True → +1"
    print("  OK test_consume_after_pick_syncs_success")


def test_both_sources_fail_triggers_refresh():
    """both sources fail → refresh_health(now) 探针"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        down = {"api_alive": False, "logged_in": False}
        svc = _make(td, daily=lambda *a, **k: None, recent=lambda *a, **k: None,
                    health=lambda: down)
        assert svc.health()["faulty"] is False  # 初始
        t = svc.peek_music_topic(NOW)
        assert t is None, "both fail → None"
        # 探针后应置 faulty
        assert svc.health()["faulty"] is True
        assert svc.health()["failure_reason"] == "unreachable"
        assert svc._health["quota_music_used"] == 0, "失败不消费"
        # healthy 探针分支：both fail 但 api正常 → 保持非 faulty
        healthy = {"api_alive": True, "logged_in": True}
        svc2 = _make(td + "_2", daily=lambda *a, **k: None, recent=lambda *a, **k: None,
                     health=lambda: healthy)
        t2 = svc2.peek_music_topic(NOW)
        assert t2 is None
        assert svc2.health()["faulty"] is False, "healthy 探针不置 faulty"
    print("  OK test_both_sources_fail_triggers_refresh")


# ── 2. 日切惰性归零 ─────────────────────────────

def test_quota_rolls_over_lazy():
    """daily 午夜惰性归零：quota_music_day != today → reset 0"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, quota=1, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        t = svc.peek_music_topic(NOW)
        assert t is not None
        svc.consume_music_topic(NOW)
        assert svc._health["quota_music_used"] == 1
        assert svc._health["quota_music_day"] == "2026-07-31"
        assert svc._music_quota_left(NOW) == 0
        assert svc.peek_music_topic(NOW) is None
        # 次日 lazy roll：peek 时 _roll_quota 触发
        t2 = svc.peek_music_topic(NEXT_DAY)
        assert t2 is not None, "跨天应重置"
        assert svc._health["quota_music_day"] == "2026-08-01"
        assert svc._health["quota_music_used"] == 0, "跨天 peek 不消费，保持 0"
        svc.consume_music_topic(NEXT_DAY)
        assert svc._health["quota_music_used"] == 1
        assert svc._health["quota_music_day"] == "2026-08-01"
        # fault 配额同理
        svc_f = _make(td + "_f", fault_quota=1, health=lambda: {"api_alive": False, "logged_in": False})
        svc_f.refresh_health(NOW)
        tf = svc_f.peek_music_topic(NOW)
        assert tf is not None
        svc_f.consume_fault_topic(NOW)
        assert svc_f._health["quota_fault_used"] == 1
        assert svc_f.peek_music_topic(NOW) is None
        tf2 = svc_f.peek_music_topic(NEXT_DAY)
        assert tf2 is not None, "fault 跨天重置"
        assert svc_f._health["quota_fault_used"] == 0
    print("  OK test_quota_rolls_over_lazy")


def test_quota_roll_does_not_use_wall_today():
    """日切不写死 date：用传入 now 的 CST 日期，非系统 today"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, quota=1, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        # 强制 health 为旧日期
        svc._health["quota_music_day"] = "2020-01-01"
        svc._health["quota_music_used"] = 99
        # 用 NOW 触发 roll → 应归零并设为 NOW 的日期
        left = svc._music_quota_left(NOW)
        assert svc._health["quota_music_day"] == "2026-07-31"
        assert svc._health["quota_music_used"] == 0
        assert left == 1
    print("  OK test_quota_roll_does_not_use_wall_today")


# ── 3. faulty 时段门禁前置 ─────────────────────────

def test_faulty_gate_time_respected():
    """faulty 时段门禁前置：in_class / in_quiet 恒 None，即使 faulty 也不发 fault"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, health=lambda: {"api_alive": False, "logged_in": False})
        svc.refresh_health(NOW)
        assert svc.health()["faulty"] is True
        assert svc.peek_music_topic(NOW, in_class=True) is None
        assert svc._health["quota_fault_used"] == 0, "门禁不消费 fault 配额"
        assert svc.peek_music_topic(NOW, in_quiet_window=True) is None
        assert svc._health["quota_fault_used"] == 0
        # 普通窗口才发
        t = svc.peek_music_topic(NOW)
        assert t is not None and t["type"] == "netease_fault"
        assert svc._health["quota_fault_used"] == 0, "peek 仍不消费"
        svc.consume_fault_topic(NOW)
        assert svc._health["quota_fault_used"] == 1
    print("  OK test_faulty_gate_time_respected")


def test_faulty_reprobe_respects_gate():
    """faulty + 需重探 时，门禁仍优先：in_class 直接 None 不探针"""
    with tempfile.TemporaryDirectory() as td:
        calls = {"health": 0}

        def fh():
            calls["health"] += 1
            return {"api_alive": False, "logged_in": False}

        svc = _make(td, health=fh)
        svc.refresh_health(NOW)
        assert svc.health()["faulty"] is True
        # 设 last_check 过期，本应重探，但 in_class 优先阻断
        svc._health["last_check"] = (NOW - timedelta(minutes=40)).isoformat()
        assert svc.peek_music_topic(NOW, in_class=True) is None
        assert calls["health"] == 1, "门禁前置，不应重探"
        assert svc.peek_music_topic(NOW, in_quiet_window=True) is None
        assert calls["health"] == 1
        # 非门禁才重探
        svc.peek_music_topic(NOW)
        assert calls["health"] == 2
    print("  OK test_faulty_reprobe_respects_gate")


# ── 4. 健康 8级降级链 ───────────────────────────

def test_health_eight_levels():
    """健康 8级降级链：覆盖 unreachable / login_expired(301) / api_error / malformed / healthy 等 8组合"""
    cases = [
        ({"api_alive": False, "logged_in": False}, "unreachable", False),
        ({"api_alive": True, "logged_in": False, "api_error": 301}, "login_expired", False),
        ({"api_alive": True, "logged_in": False, "api_error": None}, "login_expired", False),
        ({"api_alive": True, "logged_in": False, "api_error": 400}, "api_error", False),
        ({"api_alive": True, "logged_in": False, "api_error": 500}, "api_error", False),
        ({"api_alive": True, "logged_in": False, "api_error": "malformed"}, "api_error", False),
        ({"api_alive": True, "logged_in": True}, None, True),
        (None, "unreachable", False),  # bridge 返回 None
    ]
    for idx, (hresp, exp_reason, exp_healthy) in enumerate(cases):
        with tempfile.TemporaryDirectory() as td:
            br = _FakeBridge(health=lambda h=hresp: h)
            svc = NeteaseService({"netease": {"reprobe_minutes": 30}, "topic_picker": {}}, td, bridge=br)
            svc.refresh_health(NOW)
            h = svc.health()
            if exp_healthy:
                assert h["faulty"] is False and h["failure_reason"] is None, f"case {idx} {hresp} -> {h}"
                assert h["api_alive"] is True and h["logged_in"] is True
            else:
                assert h["faulty"] is True, f"case {idx} {hresp} -> {h}"
                assert h["failure_reason"] == exp_reason, f"case {idx} got {h['failure_reason']} exp {exp_reason}"
            assert h["last_check"] == NOW.isoformat()
            if exp_reason:
                assert h["last_failure"] == NOW.isoformat()
    print("  OK test_health_eight_levels")


def test_login_expired_301_chain():
    """faulty health 301→login_expired 明确，8级中 301 单独分级"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, health=lambda: {"api_alive": True, "logged_in": False, "api_error": 301})
        h = svc.refresh_health(NOW)
        assert h["failure_reason"] == "login_expired"
        svc2 = _make(td + "_2", health=lambda: {"api_alive": True, "logged_in": False, "api_error": 302})
        h2 = svc2.refresh_health(NOW)
        assert h2["failure_reason"] == "api_error", "非 301 应为 api_error"
    print("  OK test_login_expired_301_chain")


# ── 5. 锚定与权限与 DI ─────────────────────────

def test_base_dir_anchored_and_bridge_injectable():
    """保留 <base_dir>/netease/ 锚定、bridge 可注入 fake、data_dir.mkdir"""
    with tempfile.TemporaryDirectory() as td:
        # 无 netease 目录预创建
        assert not Path(td, "netease").exists()
        br = _FakeBridge(daily=lambda *a, **k: _songs())
        svc = NeteaseService({"netease": {}, "topic_picker": {}}, td, bridge=br)
        assert svc.data_dir == Path(td) / "netease"
        assert svc.health_file == Path(td) / "netease" / "netease_health.json"
        assert svc.bridge is br, "bridge 注入应保留"
        # 首次 save 触发 mkdir
        svc._health["quota_music_used"] = 0
        svc._save_health()
        assert Path(td, "netease").is_dir()
    print("  OK test_base_dir_anchored_and_bridge_injectable")


def test_health_file_0600():
    """health 文件 0600 权限"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        svc.peek_music_topic(NOW)
        svc.consume_music_topic(NOW)
        hf = Path(td) / "netease" / "netease_health.json"
        assert hf.exists()
        mode = stat.S_IMODE(hf.stat().st_mode)
        assert mode == 0o600, f"health 600 实际 {oct(mode)}"
    print("  OK test_health_file_0600")


def test_source_weights_cfg_float_clamp():
    """source_weights cfg_float clamp 0，inf/nan 回退"""
    import math
    with tempfile.TemporaryDirectory() as td:
        svc = _make(td, weights=[1, -5])
        assert svc.source_weights == [1.0, 0.0]
        svc2 = _make(td + "_2", weights=[float("inf"), 1])
        assert all(math.isfinite(w) for w in svc2.source_weights)
        svc3 = _make(td + "_3", weights=[float("nan"), 1])
        assert all(math.isfinite(w) for w in svc3.source_weights)
        svc4 = _make(td + "_4", weights=[-1, -1])
        assert svc4.source_weights == [0.5, 0.5]
    print("  OK test_source_weights_cfg_float_clamp")


def test_retry_backoff_cfg_float():
    """retry_backoff 已 cfg_float"""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {"retry_backoff_seconds": "bad"}, "topic_picker": {}}
        svc = NeteaseService(cfg, td, bridge=_FakeBridge())
        assert svc.retry_backoff == 2.0  # 非法回退默认
        cfg2 = {"netease": {"retry_backoff_seconds": -5}, "topic_picker": {}}
        svc2 = NeteaseService(cfg2, td, bridge=_FakeBridge())
        assert svc2.retry_backoff == 0.0  # clamp_min=0
    print("  OK test_retry_backoff_cfg_float")

