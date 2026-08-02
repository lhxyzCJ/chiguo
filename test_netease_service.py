#!/usr/bin/env python3
"""test_netease_service.py — chiguo_netease 策略层单元测试
(Task 2:健康文件/登录失效检测/降级链/共享日配额/随机选源/音乐话题素材组装)

monkeypatch netease_bridge 三个公开函数(fetch_daily_songs / fetch_recent_play /
check_health),全部 try/finally 恢复,不污染模块状态。所有 base_dir 用
tempfile.TemporaryDirectory,绝不触碰真实 netease_health.json。
"""

import json
import os
import random
import sys
import tempfile
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 7, 31, 22, 30, 0, tzinfo=CST)   # 周五 22:30
NEXT_DAY = datetime(2026, 8, 1, 9, 0, 0, tzinfo=CST)  # 周六 09:00(跨天)

import netease_bridge
import chiguo_netease

_DEFAULT_RETRY = (1, 2.0)  # netease_bridge 模块级重试策略默认值


# ── 构造助手 ────────────────────────────────────────────────


def _songs(n=3):
    """fetch_daily_songs 假返回(结构对齐 bridge 简化 schema)"""
    return [{"id": i, "name": f"歌{i}", "artists": "歌手"} for i in range(1, n + 1)]


def _plays():
    """fetch_recent_play 假返回"""
    return [{"playTime": 1722441600000, "name": "夜曲", "artist": "周杰伦"}]


def _make_service(td, quota=2, fault_quota=1, weights=None, enabled=True):
    """构造 NeteaseService:重试 0 次/backoff 0(测试不真实 sleep)"""
    cfg = {
        "netease": {
            "retry_count": 0, "retry_backoff_seconds": 0.0, "reprobe_minutes": 30.0,
            "enabled": enabled,
        },
        "topic_picker": {
            "netease_daily_quota": quota, "netease_fault_daily_quota": fault_quota,
        },
    }
    if weights is not None:
        cfg["topic_picker"]["netease_source_weights"] = weights
    return chiguo_netease.NeteaseService(cfg, td)


def _patch(daily=None, recent=None, health=None):
    """monkeypatch 桥接函数;返回原函数元组供 _restore 恢复"""
    orig = (netease_bridge.fetch_daily_songs,
            netease_bridge.fetch_recent_play,
            netease_bridge.check_health)
    if daily is not None:
        netease_bridge.fetch_daily_songs = daily
    if recent is not None:
        netease_bridge.fetch_recent_play = recent
    if health is not None:
        netease_bridge.check_health = health
    return orig


def _restore(orig):
    (netease_bridge.fetch_daily_songs,
     netease_bridge.fetch_recent_play,
     netease_bridge.check_health) = orig


def _down_health():
    return {"api_alive": False, "logged_in": False}


# ── 1. 健康文件 ─────────────────────────────────────────────




def test_disabled_returns_none():
    """网易云可选来源 enabled=false → music_topic 直接 None（不拉取不消费）"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, enabled=False)
        assert svc.music_topic(NOW) is None
        assert svc.music_topic(NOW, in_class=False, in_quiet_window=False) is None
    print("  OK test_disabled_returns_none")


def test_enabled_default_true():
    """enabled 缺省默认 true（向后兼容）"""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)  # enabled 未传 → True
        assert svc.enabled is True
    print("  OK test_enabled_default_true")

def test_health_file_default_when_missing():
    """无健康文件 → _default_health 结构(全部 schema 键),不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        h = svc.health()
        assert set(h.keys()) == set(chiguo_netease.HEALTH_SCHEMA_KEYS), h.keys()
        assert h["api_alive"] is None
        assert h["logged_in"] is None
        assert h["faulty"] is False
        assert h["last_check"] is None
        assert h["quota_music_used"] == 0 and h["quota_fault_used"] == 0
        assert h["quota_music_day"] == datetime.now(CST).strftime("%Y-%m-%d")
    print("  OK test_health_file_default_when_missing")


def test_health_file_corrupt_rebuild():
    """垃圾内容 / 合法 JSON 但非 dict → 加载重建默认,不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        hf = os.path.join(td, "netease_health.json")
        with open(hf, "w") as f:
            f.write("{not valid json!!")
        svc = _make_service(td)
        h = svc.health()
        assert set(h.keys()) == set(chiguo_netease.HEALTH_SCHEMA_KEYS)
        assert h["api_alive"] is None and h["faulty"] is False
        with open(hf, "w") as f:
            json.dump([1, 2, 3], f)  # 合法 JSON 但非 dict
        svc2 = _make_service(td)
        assert svc2.health()["faulty"] is False
        assert svc2.health()["quota_music_used"] == 0
    print("  OK test_health_file_corrupt_rebuild")


def test_health_file_atomic_write():
    """save 后文件存在、内容合法(原子写无 .tmp 残留);quota 字段持久化到新实例"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            assert svc.music_topic(NOW) is not None  # 触发消费 → _save_health
            hf = os.path.join(td, "netease_health.json")
            assert os.path.exists(hf)
            assert not os.path.exists(hf + ".tmp")  # 原子写无残留
            data = json.load(open(hf))
            assert data["quota_music_used"] == 1
            assert data["quota_music_day"] == "2026-07-31"
            # 新实例读回:持久化生效(直接读 _health,避免跨真实日期运行偏差)
            svc2 = _make_service(td)
            assert svc2._health["quota_music_used"] == 1
            assert svc2._health["quota_music_day"] == "2026-07-31"
        finally:
            _restore(orig)
    print("  OK test_health_file_atomic_write")


# ── 2. 配额 ─────────────────────────────────────────────────


def test_quota_shared_across_sources():
    """配额 2 两源共享:前 2 次 music_topic 非 None、第 3 次 None,quota_music_used==2"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            t1 = svc.music_topic(NOW)
            t2 = svc.music_topic(NOW)
            t3 = svc.music_topic(NOW)
            assert t1 is not None and t2 is not None
            assert t3 is None
            assert svc._health["quota_music_used"] == 2
        finally:
            _restore(orig)
    print("  OK test_quota_shared_across_sources")


def test_quota_rolls_over_day():
    """now 跨天 → 配额重置(新一天 quota_music_day 更新、used 归零重新计数)"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            assert svc.music_topic(NOW) is not None
            assert svc.music_topic(NOW) is not None
            assert svc._music_quota_left(NOW) == 0  # 当天配额耗尽
            t = svc.music_topic(NEXT_DAY)
            assert t is not None  # 跨天自动重置
            assert svc._health["quota_music_day"] == "2026-08-01"
            assert svc._health["quota_music_used"] == 1
        finally:
            _restore(orig)
    print("  OK test_quota_rolls_over_day")


def test_random_source_selection():
    """seed 固定、两源可用、配额 2000 → 抽样 2000 次,daily 比例落在 0.5±0.08
    (840~1160),证明加权随机选源而非固定选源"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, quota=2000)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            daily = 0
            for _ in range(2000):
                t = svc.music_topic(NOW)
                assert t is not None
                if t["data"]["source"] == "daily":
                    daily += 1
            assert 840 <= daily <= 1160, daily  # 0.5±0.08
        finally:
            _restore(orig)
    print("  OK test_random_source_selection")


def test_source_fallback_when_daily_down():
    """daily 源不可用 → 自动换 recent 源:产出 recent 话题且消费配额 1"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: None, recent=lambda *a, **k: _plays())
        try:
            t = svc.music_topic(NOW)
            assert t is not None and t["type"] == "netease_music"
            assert t["data"]["source"] == "recent"
            assert t["data"]["name"] == "夜曲"
            assert svc._health["quota_music_used"] == 1
        finally:
            _restore(orig)
    print("  OK test_source_fallback_when_daily_down")


def test_time_gate_in_class():
    """in_class=True → None,且不消费配额"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            assert svc.music_topic(NOW, in_class=True) is None
            assert svc._health["quota_music_used"] == 0
        finally:
            _restore(orig)
    print("  OK test_time_gate_in_class")


def test_time_gate_quiet_window():
    """in_quiet_window=True → None,且不消费配额"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            assert svc.music_topic(NOW, in_quiet_window=True) is None
            assert svc._health["quota_music_used"] == 0
        finally:
            _restore(orig)
    print("  OK test_time_gate_quiet_window")


# ── 4. 故障降级链 ───────────────────────────────────────────


def test_fault_topic_bypasses_time_gate():
    """refresh_health 置 faulty → in_class=True 且 fault 配额内 → 产出 netease_fault"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(health=_down_health)
        try:
            svc.refresh_health(NOW)
            assert svc.health()["faulty"] is True
            t = svc.music_topic(NOW, in_class=True)
            assert t is not None and t["type"] == "netease_fault"
            assert t["data"] == {"source": "fault", "reason": "unreachable"}
        finally:
            _restore(orig)
    print("  OK test_fault_topic_bypasses_time_gate")


def test_fault_quota():
    """fault 配额 1 → 第 2 次调用(仍 faulty)→ None"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, fault_quota=1)
        orig = _patch(health=_down_health)
        try:
            svc.refresh_health(NOW)
            t1 = svc.music_topic(NOW)
            assert t1 is not None and t1["type"] == "netease_fault"
            t2 = svc.music_topic(NOW)
            assert t2 is None
            assert svc._health["quota_fault_used"] == 1
        finally:
            _restore(orig)
    print("  OK test_fault_quota")


def test_login_expired_detection():
    """check_health 报 api_alive=True 但 logged_in=False → faulty=True、reason=login_expired"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(health=lambda: {"api_alive": True, "logged_in": False})
        try:
            h = svc.refresh_health(NOW)
            assert h["api_alive"] is True
            assert h["logged_in"] is False
            assert h["faulty"] is True
            assert h["failure_reason"] == "login_expired"
            assert h["last_failure"] == NOW.isoformat()
            assert h["last_check"] == NOW.isoformat()
        finally:
            _restore(orig)
    print("  OK test_login_expired_detection")


def test_faulty_fast_skip_until_reprobe():
    """faulty 且 last_check 刚更新 → music_topic 不调 check_health/fetch,直接产故障话题"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        calls = {"health": 0, "daily": 0, "recent": 0}

        def fh(*a, **k):
            calls["health"] += 1
            return _down_health()

        def fd(*a, **k):
            calls["daily"] += 1
            return _songs()

        def fr(*a, **k):
            calls["recent"] += 1
            return _plays()

        orig = _patch(daily=fd, recent=fr, health=fh)
        try:
            svc.refresh_health(NOW)
            assert svc.health()["faulty"] is True
            t = svc.music_topic(NOW)
            assert t is not None and t["type"] == "netease_fault"
            assert calls["health"] == 1   # refresh_health 那次,music_topic 未重探
            assert calls["daily"] == 0 and calls["recent"] == 0  # 无任何 fetch
        finally:
            _restore(orig)
    print("  OK test_faulty_fast_skip_until_reprobe")


def test_faulty_reprobe_after_interval():
    """last_check 早于 reprobe_minutes → refresh_health 被重新调用(重探)"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        calls = {"health": 0}

        def fh(*a, **k):
            calls["health"] += 1
            return _down_health()

        orig = _patch(health=fh)
        try:
            svc.refresh_health(NOW)  # 第一次探针(计数 1)
            svc._health["last_check"] = (NOW - timedelta(minutes=40)).isoformat()
            t = svc.music_topic(NOW + timedelta(minutes=1))  # 距 last_check 41 分钟 ≥ 30
            assert t is not None and t["type"] == "netease_fault"
            assert calls["health"] == 2  # 重探发生
        finally:
            _restore(orig)
    print("  OK test_faulty_reprobe_after_interval")


def test_recovery_after_success():
    """faulty 状态 → _pick_and_fetch 成功 → health.faulty=False、last_failure=None"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(),
                      recent=lambda *a, **k: _plays(),
                      health=_down_health)
        try:
            svc.refresh_health(NOW)
            assert svc.health()["faulty"] is True
            t = svc._pick_and_fetch(NOW)
            assert t is not None and t["type"] == "netease_music"
            h = svc.health()
            assert h["faulty"] is False
            assert h["last_failure"] is None
            assert h["failure_reason"] is None
        finally:
            _restore(orig)
    print("  OK test_recovery_after_success")


def test_fetch_failure_marks_faulty_next_call_fault_topic():
    """两源全失败 + 探针 api_alive=False → 第一次调用 None(不消费配额)且已置 faulty;
    第二次调用(同 now,未到重探间隔)→ 快速跳过直接产出故障话题"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        calls = {"health": 0, "daily": 0, "recent": 0}

        def fh(*a, **k):
            calls["health"] += 1
            return _down_health()

        def fd(*a, **k):
            calls["daily"] += 1
            return None

        def fr(*a, **k):
            calls["recent"] += 1
            return None

        orig = _patch(daily=fd, recent=fr, health=fh)
        try:
            t1 = svc.music_topic(NOW)
            assert t1 is None
            assert svc._health["quota_music_used"] == 0  # 失败不消费配额
            assert svc.health()["faulty"] is True        # 探针已判定故障
            assert svc.health()["failure_reason"] == "unreachable"
            t2 = svc.music_topic(NOW)                    # 同 now:重探间隔内 → 不重探
            assert t2 is not None and t2["type"] == "netease_fault"
            assert calls["health"] == 1                  # 只有第一次调用内的探针
        finally:
            _restore(orig)
    print("  OK test_fetch_failure_marks_faulty_next_call_fault_topic")


def test_fetch_failure_healthy_probe_silent():
    """两源全失败 + 探针 healthy(如每日推荐为空但 API 正常)→ 两次都 None、无故障话题"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        calls = {"health": 0}

        def fh(*a, **k):
            calls["health"] += 1
            return {"api_alive": True, "logged_in": True}

        orig = _patch(daily=lambda *a, **k: None, recent=lambda *a, **k: None, health=fh)
        try:
            assert svc.music_topic(NOW) is None
            assert svc.music_topic(NOW) is None
            assert svc.health()["faulty"] is False
            assert svc.health()["failure_reason"] is None
            assert svc._health["quota_music_used"] == 0
            assert calls["health"] == 2  # 每次两源全失败都重新探针
        finally:
            _restore(orig)
    print("  OK test_fetch_failure_healthy_probe_silent")


def test_health_file_bad_value_types_rebuild():
    """手写脏健康文件(配额非 int / 布尔字段非 bool)→ 加载回退默认;合法值保留;消费不崩"""
    with tempfile.TemporaryDirectory() as td:
        hf = os.path.join(td, "netease_health.json")
        with open(hf, "w") as f:
            json.dump({
                "quota_music_used": "abc",   # 非 int → 回默认
                "quota_fault_used": True,    # bool 是 int 子类 → 同样非法
                "api_alive": 1,              # 非 bool → 回默认
                "logged_in": "yes",          # 非 bool → 回默认
                "faulty": "false",           # 字符串 → 回默认
            }, f)
        svc = _make_service(td)
        assert svc._health["quota_music_used"] == 0
        assert svc._health["quota_fault_used"] == 0
        assert svc._health["api_alive"] is None
        assert svc._health["logged_in"] is None
        assert svc._health["faulty"] is False
        # 合法值正常保留
        with open(hf, "w") as f:
            json.dump({"quota_music_used": 1, "api_alive": False, "faulty": True}, f)
        svc2 = _make_service(td)
        assert svc2._health["quota_music_used"] == 1
        assert svc2._health["api_alive"] is False
        assert svc2._health["faulty"] is True
        # 脏值消费不崩(原实现 _consume_music 会抛 TypeError)
        with open(hf, "w") as f:
            json.dump({"quota_music_used": "abc"}, f)
        svc3 = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            t = svc3.music_topic(NOW)
            assert t is not None
            assert svc3._health["quota_music_used"] == 1
        finally:
            _restore(orig)
    print("  OK test_health_file_bad_value_types_rebuild")


def test_source_weights_clamped_non_negative():
    """负权重钳制为 0;两权重全 ≤0 → 回退 [0.5, 0.5](退化分布防御)"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, weights=[1, -5])
        assert svc.source_weights == [1.0, 0.0]
        svc2 = _make_service(os.path.join(td, "a"), weights=[-1, -2])
        assert svc2.source_weights == [0.5, 0.5]
        svc3 = _make_service(os.path.join(td, "b"), weights=[0, 0])
        assert svc3.source_weights == [0.5, 0.5]
    print("  OK test_source_weights_clamped_non_negative")


def test_config_invalid_values_fallback():
    """v9 审计 F-1:toml 数值非法("abc")/None → 回退默认,构造不抛 ValueError(daemon
    构造与热重载不再崩溃);合法值正常生效且负值钳制为 0"""
    cfg = {
        "netease": {"retry_count": "abc", "retry_backoff_seconds": "abc",
                    "reprobe_minutes": None},
        "topic_picker": {"netease_daily_quota": "abc", "netease_fault_daily_quota": None,
                         "netease_source_weights": ["x", "y"]},
    }
    with tempfile.TemporaryDirectory() as td:
        svc = chiguo_netease.NeteaseService(cfg, td)
        assert svc.retry_count == 1
        assert svc.retry_backoff == 2.0
        assert svc.reprobe_minutes == 30.0
        assert svc.daily_quota == 2
        assert svc.fault_quota == 1
        assert svc.source_weights == [0.5, 0.5]
        # 合法值 + 负值钳制
        cfg2 = {
            "netease": {"retry_count": 3, "retry_backoff_seconds": -1.0,
                        "reprobe_minutes": 0.5},
            "topic_picker": {"netease_daily_quota": 5, "netease_fault_daily_quota": 0,
                             "netease_source_weights": [1.0, -2.0]},
        }
        svc2 = chiguo_netease.NeteaseService(cfg2, td)
        assert svc2.retry_count == 3
        assert svc2.retry_backoff == 0.0
        assert svc2.reprobe_minutes == 0.5
        assert svc2.daily_quota == 5
        assert svc2.fault_quota == 0
        assert svc2.source_weights == [1.0, 0.0]
    print("  OK test_config_invalid_values_fallback")


def test_check_health_non_dict_resp_degrades():
    """v9 审计 F-3:check_health 响应结构异常(status/data 非 dict)→ 降级不崩:
    api_alive=True + logged_in=False 安全默认;account/profile 非 dict → 按未登录解析不崩"""
    orig_api = netease_bridge._api_get
    orig_cookie = netease_bridge._load_cookie
    netease_bridge._load_cookie = lambda: "MUSIC_U=test"
    try:
        netease_bridge._api_get = lambda *a, **k: [1, 2, 3]  # status 非 dict
        h = netease_bridge.check_health()
        assert h == {"api_alive": True, "logged_in": False, "profile": None}, h
        netease_bridge._api_get = lambda *a, **k: {"code": 200, "data": [1, 2, 3]}  # data 非 dict
        h = netease_bridge.check_health()
        assert h == {"api_alive": True, "logged_in": False, "profile": None}, h
        # account/profile 非 dict → 未登录降级,其余字段正常
        netease_bridge._api_get = lambda *a, **k: {"code": 200, "data": {
            "account": "x", "profile": ["y"], "vipType": 3}}
        h = netease_bridge.check_health()
        assert h["api_alive"] is True and h["logged_in"] is False
        assert h["user_id"] is None and h["nickname"] == "" and h["vip_type"] == 3
    finally:
        netease_bridge._api_get = orig_api
        netease_bridge._load_cookie = orig_cookie
    print("  OK test_check_health_non_dict_resp_degrades")


# ── 5. 素材组装 ─────────────────────────────────────────────


def test_fault_topic_no_link_in_data():
    """fault/daily/recent 话题的 data 均不含 share_url / 链接字段"""
    with tempfile.TemporaryDirectory() as td_f, \
         tempfile.TemporaryDirectory() as td_d, \
         tempfile.TemporaryDirectory() as td_r:
        orig = _patch(daily=lambda *a, **k: _songs(),
                      recent=lambda *a, **k: _plays(),
                      health=_down_health)
        try:
            # fault 话题
            svc_f = _make_service(td_f)
            svc_f.refresh_health(NOW)
            t_fault = svc_f.music_topic(NOW)
            assert t_fault["type"] == "netease_fault"
            assert set(t_fault["data"].keys()) == {"source", "reason"}
            # daily 话题(权重 [1,0] 强制 daily 源)
            svc_d = _make_service(td_d, weights=[1, 0])
            t_daily = svc_d.music_topic(NOW)
            assert t_daily["data"]["source"] == "daily"
            assert set(t_daily["data"].keys()) == {"source", "name", "artist"}
            # recent 话题(权重 [0,1] 强制 recent 源)
            svc_r = _make_service(td_r, weights=[0, 1])
            t_recent = svc_r.music_topic(NOW)
            assert t_recent["data"]["source"] == "recent"
            assert set(t_recent["data"].keys()) == {"source", "name", "artist"}
            for t in (t_fault, t_daily, t_recent):
                blob = json.dumps(t["data"], ensure_ascii=False)
                assert "share_url" not in blob
                assert "music.163.com" not in blob
        finally:
            _restore(orig)
    print("  OK test_fault_topic_no_link_in_data")


def test_recent_uses_newest_play():
    """plays 乱序(playTime 不同)→ 素材取 playTime 最大者"""
    random.seed(42)
    plays = [
        {"playTime": 1722441600000, "name": "更早", "artist": "手A"},
        {"playTime": 1722445200000, "name": "最新", "artist": "手B"},
        {"playTime": 1722443400000, "name": "中间", "artist": "手C"},
    ]
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: None, recent=lambda *a, **k: plays)
        try:
            t = svc.music_topic(NOW)
            assert t["data"]["source"] == "recent"
            assert t["data"]["name"] == "最新"
            assert t["data"]["artist"] == "手B"
        finally:
            _restore(orig)
    print("  OK test_recent_uses_newest_play")


# ── 7. 两阶段接口(peek 探测不消费 / consume 选中后确认) ────


def test_peek_does_not_consume_quota():
    """peek 成功产出话题但 quota_music_used 不变(可重复 peek);peek+consume 后配额 +1"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, quota=1)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            t1 = svc.peek_music_topic(NOW)
            assert t1 is not None and t1["type"] == "netease_music"
            assert svc._health["quota_music_used"] == 0, "peek 不消费配额"
            t2 = svc.peek_music_topic(NOW)
            assert t2 is not None, "配额未消费 → 可重复 peek"
            assert svc._health["quota_music_used"] == 0
            svc.consume_music_topic(NOW)
            assert svc._health["quota_music_used"] == 1, "consume 后配额 +1"
            assert svc.peek_music_topic(NOW) is None, "配额耗尽后 peek → None"
            assert svc._health["quota_music_used"] == 1
        finally:
            _restore(orig)
    print("  OK test_peek_does_not_consume_quota")


def test_peek_fault_does_not_consume_fault_quota():
    """faulty 态 peek 产出故障话题不消费 fault 配额;consume_fault_topic 后 +1;耗尽后 peek → None"""
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, fault_quota=1)
        orig = _patch(health=_down_health)
        try:
            svc.refresh_health(NOW)
            t = svc.peek_music_topic(NOW)
            assert t is not None and t["type"] == "netease_fault"
            assert svc._health["quota_fault_used"] == 0, "peek 不消费 fault 配额"
            svc.consume_fault_topic(NOW)
            assert svc._health["quota_fault_used"] == 1
            assert svc.peek_music_topic(NOW) is None, "fault 配额耗尽后 peek → None"
        finally:
            _restore(orig)
    print("  OK test_peek_fault_does_not_consume_fault_quota")


def test_peek_two_sources_down_probes_health():
    """peek 两源全失败同样走 refresh_health 探针(与 music_topic 一致):置 faulty、本轮 None"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        calls = {"health": 0}

        def fh(*a, **k):
            calls["health"] += 1
            return _down_health()

        orig = _patch(daily=lambda *a, **k: None, recent=lambda *a, **k: None, health=fh)
        try:
            assert svc.peek_music_topic(NOW) is None
            assert calls["health"] == 1, "peek 两源全失败应触发探针"
            assert svc.health()["faulty"] is True
            assert svc._health["quota_music_used"] == 0
            t = svc.peek_music_topic(NOW)  # 探针后(重探间隔内)→ 直出故障话题
            assert t is not None and t["type"] == "netease_fault"
            assert calls["health"] == 1, "重探间隔内不重复探针"
        finally:
            _restore(orig)
    print("  OK test_peek_two_sources_down_probes_health")


def test_music_topic_naive_now():
    """naive now → 按 CST 补齐不崩,正常产出话题"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td)
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            t = svc.music_topic(datetime(2026, 7, 31, 22, 30, 0))  # naive
            assert t is not None and t["type"] == "netease_music"
            assert t["data"]["name"]  # 素材非空
        finally:
            _restore(orig)
    print("  OK test_music_topic_naive_now")


def test_source_weights_from_toml():
    """构造 cfg 带 netease_source_weights=[1,0] → 只选 daily 源"""
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        svc = _make_service(td, quota=5, weights=[1, 0])
        assert svc.source_weights == [1.0, 0.0]
        orig = _patch(daily=lambda *a, **k: _songs(), recent=lambda *a, **k: _plays())
        try:
            for _ in range(5):
                t = svc.music_topic(NOW)
                assert t is not None
                assert t["data"]["source"] == "daily"
        finally:
            _restore(orig)
    print("  OK test_source_weights_from_toml")


if __name__ == "__main__":
    test_health_file_default_when_missing()
    test_health_file_corrupt_rebuild()
    test_health_file_atomic_write()
    test_quota_shared_across_sources()
    test_quota_rolls_over_day()
    test_random_source_selection()
    test_source_fallback_when_daily_down()
    test_time_gate_in_class()
    test_time_gate_quiet_window()
    test_fault_topic_bypasses_time_gate()
    test_fault_quota()
    test_login_expired_detection()
    test_faulty_fast_skip_until_reprobe()
    test_faulty_reprobe_after_interval()
    test_recovery_after_success()
    test_fetch_failure_marks_faulty_next_call_fault_topic()
    test_fetch_failure_healthy_probe_silent()
    test_health_file_bad_value_types_rebuild()
    test_source_weights_clamped_non_negative()
    test_config_invalid_values_fallback()
    test_check_health_non_dict_resp_degrades()
    test_fault_topic_no_link_in_data()
    test_recent_uses_newest_play()
    test_music_topic_naive_now()
    test_source_weights_from_toml()
    test_peek_does_not_consume_quota()
    test_peek_fault_does_not_consume_fault_quota()
    test_peek_two_sources_down_probes_health()
    netease_bridge.set_api_retry_policy(*_DEFAULT_RETRY)  # 恢复模块级重试策略
    print("test_netease_service.py: ALL PASS")
