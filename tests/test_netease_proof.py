#!/usr/bin/env python3
"""test_netease_proof.py — NeteaseBridge.fetch_recent_play 解析/缓存/降级单元测试
(Task 3 bridge 部分;daemon 联动在 Task 4 追加)"""

import json
import os
import sys
import tempfile
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from netease.bridge import NeteaseBridge

NOW = datetime(2026, 7, 31, 22, 30, 0, tzinfo=CST)  # 周五 22:30

# ── 构造助手 ────────────────────────────────────────────────


def entry(playTime, name="歌", artist="歌手"):
    return {"playTime": playTime, "song": {"name": name, "artists": [{"name": artist}]}}


def ok_resp(*entries):
    return {"code": 200, "data": {"list": list(entries)}}


def _fake_ok(fn):
    """包装 _api_get fake:记录调用次数后转发 fn(path, cookie, timeout)"""
    calls = {"n": 0}

    def fake(path, cookie=None, timeout=10):
        calls["n"] += 1
        return fn(path, cookie, timeout)

    fake.calls = calls
    return fake


def _bridge(base_dir):
    """每用例独立实例:路径注入临时目录,cookie 用临时文件(调用时解析,天然隔离)。"""
    return NeteaseBridge(base_dir, cookie_path=Path(base_dir) / "cookie.txt")


# ── 1. 成功解析 ─────────────────────────────────────────────


def test_success_parse():
    """fake _api_get 返回 2 条 → 简化条目 playTime/name/artist 正确,仅保留所需字段"""
    fake = _fake_ok(lambda path, cookie, timeout: (
        path == "/user/record?type=1&limit=20" and
        ok_resp(entry(1722441600000, "歌名A", "歌手A"),
                entry(1722445200000, "歌名B", "歌手B"))))
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        plays = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 1
        assert plays == [
            {"playTime": 1722441600000, "name": "歌名A", "artist": "歌手A"},
            {"playTime": 1722445200000, "name": "歌名B", "artist": "歌手B"},
        ]
        assert set(plays[0].keys()) == {"playTime", "name", "artist"}
    print("  OK test_success_parse")


def test_limit_propagation():
    """limit 透传到请求路径,并对结果截断"""
    seen = {}

    def fn(path, cookie, timeout):
        seen["path"] = path
        return ok_resp(*[entry(1722441600000 + i, f"歌{i}", f"手{i}") for i in range(10)])

    fake = _fake_ok(fn)
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        plays = b.fetch_recent_play(limit=3, now=NOW, cache_file=cf)
        assert seen["path"] == "/user/record?type=1&limit=3"
        assert len(plays) == 3
        assert plays[0]["playTime"] == 1722441600000
    print("  OK test_limit_propagation")


# ── 2. 缓存命中 + 副本语义 ──────────────────────────────────


def test_cache_hit_and_copy():
    """第二次调用缓存命中不触发 _api_get;返回值是副本,调用方篡改不影响后续"""
    fake = _fake_ok(lambda path, cookie, timeout: ok_resp(entry(1722441600000, "歌名A", "歌手A")))
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        first = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert first == [{"playTime": 1722441600000, "name": "歌名A", "artist": "歌手A"}]
        assert fake.calls["n"] == 1
        # 篡改返回值:先改嵌套 dict,再整体替换元素
        first[0]["name"] = "hacked"
        first[0] = {"playTime": 1, "name": "hacked", "artist": "hacked"}
        second = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 1  # 缓存命中,未再触发 API
        assert second[0]["name"] == "歌名A"  # 篡改未污染缓存
        third = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert third[0]["name"] == "歌名A"
    print("  OK test_cache_hit_and_copy")


# ── 3. 缓存过期 ─────────────────────────────────────────────


def test_cache_expiry():
    """now 超过 ttl → 重新拉取(_api_get 计数递增);恰好在 ttl 边界内 → 命中"""
    fake = _fake_ok(lambda path, cookie, timeout: ok_resp(entry(1722441600000, "歌", "手")))
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        ttl = 15
        assert b.fetch_recent_play(now=NOW, ttl_minutes=ttl, cache_file=cf)
        assert fake.calls["n"] == 1
        # 16 分钟 → 过期,重新拉取
        assert b.fetch_recent_play(
            now=NOW + timedelta(minutes=16), ttl_minutes=ttl, cache_file=cf)
        assert fake.calls["n"] == 2
        # 恰好 15 分钟(边界)→ 命中
        assert b.fetch_recent_play(
            now=NOW + timedelta(minutes=31), ttl_minutes=ttl, cache_file=cf)
        assert fake.calls["n"] == 2
        # 再超 1 分钟 → 重新拉取
        assert b.fetch_recent_play(
            now=NOW + timedelta(minutes=32), ttl_minutes=ttl, cache_file=cf)
        assert fake.calls["n"] == 3
    print("  OK test_cache_expiry")


# ── 4. API 失败 / 缓存损坏 ──────────────────────────────────


def test_api_failure_and_corrupt_cache():
    """API 失败 → None 且不写缓存;垃圾 JSON / 结构异常缓存 → 视为未缓存,重新拉取不崩"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        cf = os.path.join(td, "recent_play_cache.json")
        # API 失败 → None,不缓存失败
        b._api_get = lambda *a, **k: None
        assert b.fetch_recent_play(now=NOW, cache_file=cf) is None
        assert not os.path.exists(cf)
        # 垃圾 JSON → 视为未缓存,重新拉取不崩
        fake = _fake_ok(lambda path, cookie, timeout: ok_resp(entry(1722441600000, "歌名A", "歌手A")))
        b._api_get = fake
        with open(cf, "w") as f:
            f.write("{not valid json!!")
        r = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 1
        assert r == [{"playTime": 1722441600000, "name": "歌名A", "artist": "歌手A"}]
        # 合法 JSON 但结构异常(缺 plays / 缺 fetched_at / 非 dict)→ 同样视为未缓存
        with open(cf, "w") as f:
            json.dump({"foo": "bar"}, f)
        r = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 2 and r is not None
        with open(cf, "w") as f:
            json.dump([1, 2, 3], f)  # 非 dict
        r = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 3 and r is not None
        with open(cf, "w") as f:
            json.dump({"plays": []}, f)  # 缺 fetched_at
        r = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 4 and r is not None
    print("  OK test_api_failure_and_corrupt_cache")


# ── 5. 非法条目过滤 / 错误码 / 结构缺失 ─────────────────────


def test_invalid_playtime_filtered_and_error_codes():
    """非法 playTime(字符串/缺失/None)与非 dict 条目 → 逐条过滤;code!=200 → None;
    data.list 缺失或非列表 → None;空列表 → []"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        bad_entries = [
            entry(1722441600000, "好歌", "好歌手"),
            {"playTime": "abc", "song": {"name": "坏1", "artists": []}},        # 字符串
            {"song": {"name": "坏2", "artists": [{"name": "x"}]}},               # 缺失
            {"playTime": None, "song": {"name": "坏3"}},                         # None
            {"playTime": 1722445200000, "song": {"name": "无歌手", "artists": []}},  # 无 artist → 未知
            "garbage",                                                           # 非 dict 条目
        ]
        fake = _fake_ok(lambda path, cookie, timeout: ok_resp(*bad_entries))
        b._api_get = fake
        plays = b.fetch_recent_play(now=NOW, cache_file=os.path.join(td, "a.json"))
        assert plays is not None and len(plays) == 2
        assert plays[0] == {"playTime": 1722441600000, "name": "好歌", "artist": "好歌手"}
        assert plays[1] == {"playTime": 1722445200000, "name": "无歌手", "artist": "未知"}
        # code != 200(301 未登录 / 500)→ None
        b._api_get = lambda *a, **k: {"code": 301, "message": "需要登录"}
        assert b.fetch_recent_play(now=NOW, cache_file=os.path.join(td, "b.json")) is None
        b._api_get = lambda *a, **k: {"code": 500, "message": "boom"}
        assert b.fetch_recent_play(now=NOW, cache_file=os.path.join(td, "c.json")) is None
        # data.list 缺失 / 非列表 → None
        b._api_get = lambda *a, **k: {"code": 200, "data": {}}
        assert b.fetch_recent_play(now=NOW, cache_file=os.path.join(td, "d.json")) is None
        b._api_get = lambda *a, **k: {"code": 200, "data": {"list": {"x": 1}}}
        assert b.fetch_recent_play(now=NOW, cache_file=os.path.join(td, "e.json")) is None
        # 空列表 → [] (成功但无记录)
        b._api_get = lambda *a, **k: {"code": 200, "data": {"list": []}}
        plays = b.fetch_recent_play(now=NOW, cache_file=os.path.join(td, "f.json"))
        assert plays == []
    print("  OK test_invalid_playtime_filtered_and_error_codes")


def test_non_dict_resp_and_data_return_none():
    """resp 或 data 非 dict → 解析失败语义,返回 None 不抛 AttributeError(不写缓存)"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        cf = os.path.join(td, "recent_play_cache.json")
        b._api_get = lambda *a, **k: [1, 2, 3]  # resp 非 dict
        assert b.fetch_recent_play(now=NOW, cache_file=cf) is None
        b._api_get = lambda *a, **k: {"code": 200, "data": [1, 2, 3]}  # data 非 dict
        assert b.fetch_recent_play(now=NOW, cache_file=cf) is None
        assert not os.path.exists(cf)
    print("  OK test_non_dict_resp_and_data_return_none")


# ── 6. 缓存路径注入 ─────────────────────────────────────────


def test_cache_path_injection():
    """缓存路径注入临时目录:内容格式 {fetched_at iso(CST), plays};不触碰模块目录真实文件;
    不同缓存文件互不共享"""
    real_file = str(NeteaseBridge(Path(__file__).resolve().parent.parent).recent_play_cache_file)
    # v9 审计 F-2:生产缓存可能合法存在(daemon 运行写入),断言改为「测试不写生产路径」——
    # 记录测试前生产文件内容快照(不存在则记录 None),跑完后必须逐字节一致
    real_snap = None
    if os.path.exists(real_file):
        with open(real_file, "rb") as f:
            real_snap = f.read()
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = _fake_ok(lambda path, cookie, timeout: ok_resp(entry(1722441600000, "歌", "手")))
        cf = os.path.join(td, "recent_play_cache.json")
        plays = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert plays is not None
        assert os.path.exists(cf)
        data = json.loads(open(cf).read())
        assert set(data.keys()) == {"fetched_at", "plays"}
        assert data["fetched_at"] == NOW.isoformat()  # 带 +08:00
        assert data["plays"] == [{"playTime": 1722441600000, "name": "歌", "artist": "手"}]
        # 测试未触碰真实模块目录文件(快照逐字节未变/依旧不存在)
        if real_snap is None:
            assert not os.path.exists(real_file), "测试不得写生产缓存文件"
        else:
            with open(real_file, "rb") as f:
                assert f.read() == real_snap, "测试不得改写生产缓存文件"
        # 不同缓存文件 → 独立缓存(重新拉取)
        cf2 = os.path.join(td, "other.json")
        assert b.fetch_recent_play(now=NOW, cache_file=cf2) is not None
        assert not os.path.exists(os.path.join(td, "recent_play_cache.json.tmp"))
    print("  OK test_cache_path_injection")


# ── 7. 未登录 / 登录过期 ────────────────────────────────────


def test_not_logged_in():
    """_load_cookie → None → 直接返回 None,不发 API;已登录但服务端 301 → None"""
    calls = {"n": 0}

    def fn(path, cookie, timeout):
        calls["n"] += 1
        return ok_resp(entry(1, "x", "y"))

    fake = _fake_ok(fn)
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: None
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        assert b.fetch_recent_play(now=NOW, cache_file=cf) is None
        assert fake.calls["n"] == 0  # 未登录不发请求
        # 已登录但服务端 301(登录过期)→ None
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = lambda *a, **k: {"code": 301, "message": "需要登录"}
        assert b.fetch_recent_play(now=NOW, cache_file=cf) is None
        assert not os.path.exists(cf)  # 失败不缓存
    print("  OK test_not_logged_in")


def test_future_fetched_at_not_hit():
    """未来 fetched_at(时钟回拨/篡改缓存)→ 负年龄 → 不命中,重新拉取"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        cf = os.path.join(td, "recent_play_cache.json")
        future = {"fetched_at": (NOW + timedelta(hours=1)).isoformat(),
                  "plays": [{"playTime": 1, "name": "未来", "artist": "未来"}]}
        with open(cf, "w") as f:
            json.dump(future, f)
        # 篡改的缓存不命中;但随后正常的拉取会写正常缓存
        b._api_get = fake = _fake_ok(
            lambda path, cookie, timeout: ok_resp(entry(1722441600000, "真实", "歌手")))
        plays = b.fetch_recent_play(now=NOW, ttl_minutes=15, cache_file=cf)
        assert fake.calls["n"] == 1  # 重新拉取
        assert plays[0]["name"] == "真实"
        # 拉取后缓存被正常覆盖 → 再调命中
        plays2 = b.fetch_recent_play(now=NOW, ttl_minutes=15, cache_file=cf)
        assert fake.calls["n"] == 1
        assert plays2[0]["name"] == "真实"
    print("  OK test_future_fetched_at_not_hit")


def test_song_not_dict_does_not_crash():
    """song 为 truthy 非 dict(如字符串)→ 不崩,条目保留,name 空/artist 未知"""
    fake = _fake_ok(lambda path, cookie, timeout: ok_resp(
        {"playTime": 1722441600000, "song": "x"}))  # song: "x"
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        plays = b.fetch_recent_play(now=NOW, cache_file=cf)
        assert fake.calls["n"] == 1
        assert plays is not None and len(plays) == 1
        assert plays[0]["playTime"] == 1722441600000
        assert plays[0]["name"] == ""
        assert plays[0]["artist"] == "未知"
    print("  OK test_song_not_dict_does_not_crash")


# ── 8. now 参数 / tz 补齐 / 默认值 ──────────────────────────


def test_naive_now_tz_padding():
    """naive now → 按 CST 补齐;缓存文件 fetched_at 带 +08:00;naive 与 aware 同刻命中"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = _fake_ok(lambda path, cookie, timeout: ok_resp(entry(1722441600000, "歌", "手")))
        cf = os.path.join(td, "recent_play_cache.json")
        naive = datetime(2026, 7, 31, 22, 30, 0)
        assert b.fetch_recent_play(now=naive, cache_file=cf) is not None
        data = json.loads(open(cf).read())
        assert data["fetched_at"] == "2026-07-31T22:30:00+08:00"
        # naive 与 aware 同刻 → 命中
        assert b.fetch_recent_play(now=NOW, cache_file=cf) is not None
    print("  OK test_naive_now_tz_padding")


def test_default_now_smoke():
    """now=None → datetime.now(CST);两次连续调用走缓存;naive 无 tz 的 fetched_at 也能命中"""
    fake = _fake_ok(lambda path, cookie, timeout: ok_resp(entry(1722441600000, "歌", "手")))
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._api_get = fake
        cf = os.path.join(td, "recent_play_cache.json")
        r1 = b.fetch_recent_play(ttl_minutes=15, cache_file=cf)
        assert r1 is not None and fake.calls["n"] == 1
        r2 = b.fetch_recent_play(ttl_minutes=15, cache_file=cf)
        assert fake.calls["n"] == 1  # 缓存命中
        assert r1 == r2
        # 手工写入无 tz 的旧格式缓存(模拟历史文件)→ 补齐 tz 后命中
        old = {"fetched_at": "2026-07-31T22:30:00", "plays": [{"playTime": 1, "name": "旧", "artist": "旧"}]}
        with open(cf, "w") as f:
            json.dump(old, f)
        r3 = b.fetch_recent_play(
            now=datetime(2026, 7, 31, 22, 31, 0, tzinfo=CST), ttl_minutes=15, cache_file=cf)
        assert fake.calls["n"] == 1  # 补齐 tz 后仍在 ttl 内 → 命中
        assert r3 == old["plays"]
    print("  OK test_default_now_smoke")


# ════════════════════════════════════════════════════════════
# Task 4: daemon 联动 (DecisionEngine._check_play_proof + sleeping 压制)
# ════════════════════════════════════════════════════════════

import re

import chiguo_daemon
from chiguo_math import in_quiet_window
from chiguo_circadian import bucket_for

DAEMON_NOW = datetime(2026, 8, 1, 1, 30, 0, tzinfo=CST)  # 周六 01:30(窗口 0-8 内)


def _toml_variant(factor=None):
    """真实 toml 副本;factor 不为 None 时附加 [netease] sleeping_confidence_factor=factor"""
    txt = Path("chiguo_proactive.toml").read_text()
    txt = re.sub(r"\[netease\][^\[]*", "", txt, flags=re.S)  # 去旧 [netease] 段,防重复表
    if factor is None:
        return txt
    return (txt + "\n[netease]\n"
            "play_cache_ttl_minutes = 15\n"
            "play_proof_window_hours = 2.0\n"
            f"sleeping_confidence_factor = {factor}\n")


def _make_engine(tmp, factor=None):
    """临时目录 + 真实 toml 副本 → DecisionEngine,固定测试静默窗口 0-8"""
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    txt = _toml_variant(factor)
    # 隔离:mem0_qdrant_path 改写为临时目录,防止新机器上连到生产记忆库
    txt = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(tmp) / "no_qdrant"}"', txt)
    txt = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{Path(tmp) / "no_history.db"}"', txt)
    cfg_path.write_text(txt)
    engine = chiguo_daemon.DecisionEngine(str(cfg_path), str(Path(tmp) / "decisions.jsonl"))
    engine.state.cooldown.set_quiet_window(0, 8)  # 固定测试窗口,不受 circadian 学习影响
    # A3: fetch_play_proof 拉取后按 _should_reprobe 刷新 health;桩 check_health 防真实网络
    engine.netease_service.bridge.check_health = lambda: {"api_alive": True, "logged_in": True}
    return engine


def _patch_engine_fetch(engine, result):
    """按 engine 实例桩 bridge.fetch_recent_play(daemon 经 netease_service 单入口)"""
    bridge = engine.netease_service.bridge
    orig = bridge.fetch_recent_play
    calls = {"n": 0}

    def fake(*a, **k):
        calls["n"] += 1
        return result

    bridge.fetch_recent_play = fake
    return orig, calls


def _sleeping_state(engine, now):
    """伪造 Bayesian sleeping 高置信 0.8(真实 infer 在近期交互下不会给 sleeping)"""
    st = engine.state
    st.cooldown.last_user_message_at = (now - timedelta(hours=2)).isoformat()
    st.cooldown.last_message_at = (now - timedelta(hours=3)).isoformat()
    st.cooldown.messages_today = 0
    st.cooldown.current_date = now.strftime("%Y-%m-%d")
    st.infer_user_state = lambda now=None, msg_length=None: {
        "posterior": {"sleeping": 0.9, "browsing": 0.05, "busy": 0.05},
        "most_likely": "sleeping", "confidence": 0.8, "utility": 0.1,
        "should_send_bayesian": False, "state_description": "sleeping",
    }


def test_quiet_window_boundaries():
    """in_quiet_window 跨午夜语义:0-8 → 0:00 True/8:00 False/23:00 False;22-8 → 23:00/7:00 True/8:00/12:00 False"""
    assert in_quiet_window(datetime(2026, 8, 1, 0, 0, tzinfo=CST), 0, 8)
    assert not in_quiet_window(datetime(2026, 8, 1, 8, 0, tzinfo=CST), 0, 8)
    assert not in_quiet_window(datetime(2026, 8, 1, 23, 0, tzinfo=CST), 0, 8)
    assert in_quiet_window(datetime(2026, 8, 1, 23, 0, tzinfo=CST), 22, 8)
    assert in_quiet_window(datetime(2026, 8, 1, 7, 0, tzinfo=CST), 22, 8)
    assert not in_quiet_window(datetime(2026, 8, 1, 8, 0, tzinfo=CST), 22, 8)
    assert not in_quiet_window(datetime(2026, 8, 1, 12, 0, tzinfo=CST), 22, 8)
    print("  OK test_quiet_window_boundaries")


def test_check_play_proof_in_window_with_recent_play():
    """窗口内 + 1h 前播放 → play_proof True;record_active 生效(active_days 增长到窗口内小时)"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now_ms = int(DAEMON_NOW.timestamp() * 1000)
        orig, calls = _patch_engine_fetch(engine, [{"playTime": now_ms - 3600_000, "name": "夜曲", "artist": "周杰伦"}])
        try:
            assert engine._check_play_proof(DAEMON_NOW) is True
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] == 1
        days = engine.state.circadian.active_days
        assert len(days) == 1, days
        assert days[0]["hours"] == [0]  # 1h 前 = 0:00,窗口内
        assert days[0]["bucket"] == "weekend"  # 周六 → weekend 桶
    print("  OK test_check_play_proof_in_window_with_recent_play")


def test_check_play_proof_recompute_called():
    """play_proof 成立 → circadian.recompute 恰好调用 1 次;无证据 → 不再调用"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now_ms = int(DAEMON_NOW.timestamp() * 1000)
        orig_recompute = engine.state.circadian.recompute
        calls = {"n": 0}

        def wrapped(*a, **k):
            calls["n"] += 1
            return orig_recompute(*a, **k)

        engine.state.circadian.recompute = wrapped
        orig, _ = _patch_engine_fetch(engine, [{"playTime": now_ms - 3600_000, "name": "夜曲", "artist": "周杰伦"}])
        try:
            assert engine._check_play_proof(DAEMON_NOW) is True
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] == 1, calls
        # 对照: 无近播放证据(5h 前)→ 不触发 recompute
        orig2, _ = _patch_engine_fetch(engine, [{"playTime": now_ms - 5 * 3600_000, "name": "旧", "artist": "旧"}])
        try:
            assert engine._check_play_proof(DAEMON_NOW) is False
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig2
        assert calls["n"] == 1, calls
    print("  OK test_check_play_proof_recompute_called")


def test_check_play_proof_buckets_by_play_time():
    """按播放时刻分桶(非评估时刻):周五 19:30 播放 + 21:30 评估(窗口 19-7)→ weekday 桶,
    旧实现按评估时刻会记成 weekend,污染双桶学习"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        engine.state.cooldown.set_quiet_window(19, 7)  # 跨午夜窗口,覆盖周五 20:00 桶边界
        eval_dt = datetime(2026, 7, 31, 21, 30, tzinfo=CST)  # 周五 21:30(评估时刻桶=weekend)
        play_dt = datetime(2026, 7, 31, 19, 30, tzinfo=CST)  # 周五 19:30(播放时刻桶=weekday)
        orig, calls = _patch_engine_fetch(engine, [{"playTime": int(play_dt.timestamp() * 1000), "name": "x", "artist": "y"}])
        try:
            assert engine._check_play_proof(eval_dt) is True
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] == 1
        days = engine.state.circadian.active_days
        assert len(days) == 1, days
        assert days[0]["hours"] == [19]
        assert days[0]["bucket"] == "weekday", days  # 播放时刻桶,非评估时刻的 weekend
    print("  OK test_check_play_proof_buckets_by_play_time")


def test_check_play_proof_outside_window_no_fetch():
    """14:00(窗口外)→ fetch 不被调用(计数 0),play_proof False,无 record_active"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        orig, calls = _patch_engine_fetch(engine, [{"playTime": 1, "name": "x", "artist": "y"}])
        try:
            outside = datetime(2026, 8, 1, 14, 0, tzinfo=CST)
            assert engine._check_play_proof(outside) is False
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] == 0
        assert engine.state.circadian.active_days == []
    print("  OK test_check_play_proof_outside_window_no_fetch")


def test_check_play_proof_stale_play_no_proof():
    """窗口内但播放 5h 前(超出 2h 证据窗)→ play_proof False,不 record_active"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now_ms = int(DAEMON_NOW.timestamp() * 1000)
        orig, calls = _patch_engine_fetch(engine, [{"playTime": now_ms - 5 * 3600_000, "name": "旧", "artist": "旧"}])
        try:
            assert engine._check_play_proof(DAEMON_NOW) is False
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] == 1  # 窗口内会拉取,但无近播放证据
        assert engine.state.circadian.active_days == []
    print("  OK test_check_play_proof_stale_play_no_proof")


def test_check_play_proof_fetch_none_or_exception():
    """fetch 返回 None / 抛异常 → 不崩,play_proof False,不 record_active"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        bridge = engine.netease_service.bridge
        orig = bridge.fetch_recent_play
        bridge.fetch_recent_play = lambda *a, **k: None
        try:
            assert engine._check_play_proof(DAEMON_NOW) is False
        finally:
            bridge.fetch_recent_play = orig

        def boom(*a, **k):
            raise RuntimeError("api down")

        bridge.fetch_recent_play = boom
        try:
            assert engine._check_play_proof(DAEMON_NOW) is False
        finally:
            bridge.fetch_recent_play = orig
        assert engine.state.circadian.active_days == []
    print("  OK test_check_play_proof_fetch_none_or_exception")


def test_check_play_proof_play_outside_quiet_window():
    """播放时间在窗口外(7/31 23:30,不在 0-8)但恰在 2h 证据窗边界内 → 不算反证,不 record_active"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now_ms = int(DAEMON_NOW.timestamp() * 1000)
        play_2330 = now_ms - int(2.0 * 3600_000)  # 2026-07-31 23:30(= 01:30 减 2h)
        orig, calls = _patch_engine_fetch(engine, [{"playTime": play_2330, "name": "夜", "artist": "夜"}])
        try:
            assert engine._check_play_proof(DAEMON_NOW) is False
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] == 1
        assert engine.state.circadian.active_days == []
    print("  OK test_check_play_proof_play_outside_quiet_window")


def test_evaluate_play_proof_suppresses_sleeping_confidence():
    """sleeping conf 0.8: 无播放证据 → 门控阻塞(idle user_sleeping);有证据 → ×0.5=0.4 ≤0.5 → 放行"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td, factor=0.5)
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0  # 空窗口:can_send 不受真实时刻静默时段影响
        engine.state._sync_quiet_window()
        _sleeping_state(engine, datetime.now(CST))  # 相对真实时刻,避免时钟倒退/未来时间戳
        # 对照: 无播放证据 → sleeping 门控阻塞
        engine._apply_play_proof = lambda now, plays: False
        d = engine.evaluate()
        assert d["action"] == "idle" and d.get("reason") in ("user_sleeping", "sleeping_guard"), d
        # 有播放证据 → effective 0.8×0.5=0.4 ≤ 0.5 → 不被 sleeping 阻塞
        engine._apply_play_proof = lambda now, plays: True
        d = engine.evaluate()
        assert d["action"] == "send" or d.get("reason") not in ("user_sleeping", "sleeping_guard"), d
    print("  OK test_evaluate_play_proof_suppresses_sleeping_confidence")


def test_toml_sleeping_confidence_factor_drives_effective():
    """toml [netease] sleeping_confidence_factor=1.0(不压制)→ 有播放证据仍阻塞;0.5 → 放行"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td, factor=1.0)
        assert engine.config["netease"]["sleeping_confidence_factor"] == 1.0
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        _sleeping_state(engine, datetime.now(CST))
        engine._apply_play_proof = lambda now, plays: True
        d = engine.evaluate()
        assert d["action"] == "idle" and d.get("reason") in ("user_sleeping", "sleeping_guard"), d
    print("  OK test_toml_sleeping_confidence_factor_drives_effective")


def test_pick_netease_only_injection_rules():
    """v9 审计 F-4:非孤独触发(_build_context)→ pick_netease_only 被调用且注入 topic;
    孤独触发走既有 pick() 路径;follow_up/reflect/lonely_high/longing 不调用不注入"""
    import chiguo_topics
    from chiguo_trigger import Trigger
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        orig_pno = chiguo_topics.TopicPicker.pick_netease_only
        calls = {"n": 0}
        fake_topic = {"type": "netease_music", "hint": "哥哥最近在听什么", "tone": "casual",
                      "data": {"source": "daily", "name": "x", "artist": "y"}}

        def fake_pno(self, now):
            calls["n"] += 1
            return dict(fake_topic)

        chiguo_topics.TopicPicker.pick_netease_only = fake_pno
        try:
            # 非孤独触发(playful)→ 调用 1 次且注入 topic
            t = Trigger(type="playful")
            engine._build_context(t, DAEMON_NOW)
            assert calls["n"] == 1, calls
            assert t.data.get("topic") is not None and t.data["topic"]["type"] == "netease_music"
            # 孤独低强度 → 既有 pick() 路径,不调 pick_netease_only
            engine.state.cooldown.trigger_history.extend(["lonely_low"] * 3)  # 强制话题分支
            t2 = Trigger(type="lonely_low")
            engine._build_context(t2, DAEMON_NOW)
            assert calls["n"] == 1, calls
            # 排除列表:follow_up / reflect / lonely_high / longing
            for tt in ("follow_up", "reflect", "lonely_high", "longing"):
                t3 = Trigger(type=tt)
                engine._build_context(t3, DAEMON_NOW)
                assert calls["n"] == 1, f"{tt} 不应调用 pick_netease_only"
                assert not t3.data.get("topic"), f"{tt} 不应注入 topic"
        finally:
            chiguo_topics.TopicPicker.pick_netease_only = orig_pno
    print("  OK test_pick_netease_only_injection_rules")


def test_evaluate_syncs_quiet_window_to_current_bucket():
    """evaluate 每次调用内部 _sync_quiet_window:手动把 cooldown 窗口设成非当前桶窗口 →
    evaluate 后窗口被刷新为当前时刻所在桶的学习窗口(loop 模式跨桶翻转即时生效)。
    桶窗口:weekday (0,5,0.9) / weekend (2,7,0.8),按真实 now 动态断言(节假日/周五晚边界不依赖系统日期)"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        st.circadian.weekday_quiet_start, st.circadian.weekday_quiet_end, st.circadian.weekday_confidence = 0, 5, 0.9
        st.circadian.weekend_quiet_start, st.circadian.weekend_quiet_end, st.circadian.weekend_confidence = 2, 7, 0.8
        real_now = datetime.now(CST)
        expected_bucket = bucket_for(real_now,
                                     st.holiday_parser.is_holiday,
                                     st.holiday_parser.is_makeup_workday)
        expected = {"weekday": (0, 5), "weekend": (2, 7)}[expected_bucket]
        # evaluate 前手动设成另一桶窗口 → 证明 evaluate 内被刷新
        st.cooldown.set_quiet_window(*({"weekday": (2, 7), "weekend": (0, 5)}[expected_bucket]))
        engine._apply_play_proof = lambda now, plays: False  # 不应用听歌反证
        # 近期消息 → can_send False(min_interval 阻塞),走 idle 分支,不污染 send 状态
        st.cooldown.last_message_at = (real_now - timedelta(minutes=10)).isoformat()
        st.cooldown.current_date = real_now.strftime("%Y-%m-%d")
        d = engine.evaluate()
        assert d["action"] == "idle", d
        assert st.cooldown.quiet_window() == expected, (
            f"evaluate 后窗口应为 {expected_bucket} 桶 {expected},实际 {st.cooldown.quiet_window()}")
    print("  OK test_evaluate_syncs_quiet_window_to_current_bucket")


# ════════════════════════════════════════════════════════════
# B1 (#136): 播放反证→放行发送（死逻辑修复——播放证明绕行 quiet-window gate）
# ════════════════════════════════════════════════════════════


def test_can_send_quiet_ok_bypasses_quiet_window():
    """窗口内播放反证成立 → can_send(now, quiet_ok=True) 放行;默认 quiet_ok=False(现状)仍 False"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        engine.state.cooldown.set_quiet_window(0, 8)
        assert in_quiet_window(DAEMON_NOW, 0, 8)  # 周六 01:30,窗口内
        now_ms = int(DAEMON_NOW.timestamp() * 1000)
        # 构造播放证明:窗口内 1h 前播放
        orig, calls = _patch_engine_fetch(
            engine, [{"playTime": now_ms - 3600_000, "name": "夜曲", "artist": "周杰伦"}])
        try:
            play_proof = engine._check_play_proof(DAEMON_NOW)
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert play_proof is True, play_proof
        # 现状(默认 quiet_ok=False): 窗口内仍被 quiet 门禁拦截
        assert engine.state.can_send(DAEMON_NOW) is False
        assert engine.state.can_send(DAEMON_NOW, quiet_ok=False) is False
        # 反证成立: quiet_ok=True → 绕过 quiet-window gate,其余 gate 全放行
        assert engine.state.can_send(DAEMON_NOW, quiet_ok=True) is True
    print("  OK test_can_send_quiet_ok_bypasses_quiet_window")


def test_evaluate_play_proof_bypasses_quiet_window():
    """窗口内播放反证成立 → evaluate 不再因 quiet-window gate 返回 idle(quiet_hours)。
    经 config [schedule] 注入覆盖真实 now 与 now-1h 的窗口(h-1,h+1)——
    evaluate 内 _load 会重建 cooldown,须由 _sync_quiet_window 回退 config 重注(cooldown 直接 set
    会被 _load 复位)。"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        real_cpp = engine._apply_play_proof
        real_now = datetime.now(CST)
        qs = (real_now.hour - 1) % 24
        qe = (real_now.hour + 1) % 24
        engine.config["schedule"]["quiet_start"] = qs
        engine.config["schedule"]["quiet_end"] = qe
        engine.state._sync_quiet_window()  # 回退注入 cooldown 窗口
        assert engine.state.cooldown.quiet_window() == (qs, qe)
        assert in_quiet_window(real_now, qs, qe)
        # 对照: 无播放证据 → 窗口内 can_send 被 quiet 门禁拦截 → idle(quiet_hours)
        engine._apply_play_proof = lambda now, plays: False
        d = engine.evaluate()
        assert d["action"] == "idle" and d.get("reason") == "quiet_hours", d
        # 有播放证据(窗口内 1h 前播放)→ quiet_ok=True 绕过门禁 → 不再 idle(quiet_hours)
        engine._apply_play_proof = real_cpp
        orig, calls = _patch_engine_fetch(
            engine, [{"playTime": int((real_now - timedelta(hours=1)).timestamp() * 1000),
                      "name": "夜曲", "artist": "周杰伦"}])
        try:
            d2 = engine.evaluate()
        finally:
            engine.netease_service.bridge.fetch_recent_play = orig
        assert calls["n"] >= 1, calls
        assert d2.get("reason") != "quiet_hours", d2
    print("  OK test_evaluate_play_proof_bypasses_quiet_window")


# ════════════════════════════════════════════════════════════
# v9 Task 1: 桥接层加固 (_api_get 有限重试 + fetch_daily_songs schema 过滤)
# ════════════════════════════════════════════════════════════


class _FakeResp:
    """fake urlopen 响应:read() 返回 payload,支持 with 语法"""

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _patch_urlopen(*behaviors):
    """monkeypatch urllib.request.urlopen:按序产出 behaviors(异常实例 → raise,bytes → 响应体,
    超出列表后重复最后一项)。返回 (orig, calls),calls["n"] 记录 urlopen 调用次数"""
    calls = {"n": 0}
    real = urllib.request.urlopen

    def fake(*a, **k):
        calls["n"] += 1
        b = behaviors[min(calls["n"] - 1, len(behaviors) - 1)]
        if isinstance(b, Exception):
            raise b
        return _FakeResp(b)

    urllib.request.urlopen = fake
    return real, calls


def test_api_get_retries_transient_failure():
    """第一次 urlopen 抛 URLError → 重试 → 第二次成功 → 返回 JSON,urlopen 恰好调用 2 次"""
    with tempfile.TemporaryDirectory() as td:
        b = NeteaseBridge(td, retry_count=1, retry_backoff=0.0)  # backoff=0,避免真实 sleep
        real, calls = _patch_urlopen(urllib.error.URLError("conn refused"), b'{"code": 200}')
        try:
            assert b._api_get("/x") == {"code": 200}
            assert calls["n"] == 2
        finally:
            urllib.request.urlopen = real
    print("  OK test_api_get_retries_transient_failure")


def test_api_get_no_retry_on_http_4xx():
    """HTTPError(403) → 非瞬时失败,立即 None,urlopen 只调 1 次"""
    with tempfile.TemporaryDirectory() as td:
        b = NeteaseBridge(td, retry_count=1, retry_backoff=0.0)
        real, calls = _patch_urlopen(
            urllib.error.HTTPError("http://x/", 403, "Forbidden", {}, None))
        try:
            assert b._api_get("/x") is None
            assert calls["n"] == 1
        finally:
            urllib.request.urlopen = real
    print("  OK test_api_get_no_retry_on_http_4xx")


def test_api_get_retries_http_5xx():
    """HTTPError(503) → 瞬时失败,重试后成功,urlopen 恰好调用 2 次"""
    with tempfile.TemporaryDirectory() as td:
        b = NeteaseBridge(td, retry_count=1, retry_backoff=0.0)
        real, calls = _patch_urlopen(
            urllib.error.HTTPError("http://x/", 503, "Service Unavailable", {}, None),
            b'{"code": 200}')
        try:
            assert b._api_get("/x") == {"code": 200}
            assert calls["n"] == 2
        finally:
            urllib.request.urlopen = real
    print("  OK test_api_get_retries_http_5xx")


def test_api_get_retry_policy_zero():
    """retry_count=0 → 不重试,URLError 立即 None(urlopen 只调 1 次)"""
    with tempfile.TemporaryDirectory() as td:
        b = NeteaseBridge(td, retry_count=0, retry_backoff=0.0)
        real, calls = _patch_urlopen(urllib.error.URLError("down"))
        try:
            assert b._api_get("/x") is None
            assert calls["n"] == 1
        finally:
            urllib.request.urlopen = real
    print("  OK test_api_get_retry_policy_zero")


def test_daily_songs_schema_filter():
    """raw_songs 混合合法 dict / 非 dict / ar 非 list / id 缺失或非 int → 仅保留合法条目,
    且合法条目字段结构正确(artists 未知 / 非 int dt·fee → 0 / al 非 dict → 空)"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._load_cache = lambda: None  # 不读真实缓存文件
        saved = []
        b._save_cache = lambda songs: saved.append(songs)  # 不写真实缓存文件
        raw = [
            {"id": 1, "name": "歌A", "ar": [{"name": "手A"}, {"name": "手B"}],
             "al": {"name": "专A", "picUrl": "http://pic/1"}, "dt": 200000, "fee": 0},
            "garbage",                                                       # 非 dict → 过滤
            {"id": 2, "name": "歌B", "ar": "notalist", "al": {"name": "专B"}},  # ar 非 list → 未知
            {"name": "歌C", "ar": [{"name": "手C"}]},                         # id 缺失 → 过滤
            {"id": "x", "name": "歌D", "ar": []},                             # id 非 int → 过滤
            {"id": 3, "name": "歌E", "ar": ["x", {"name": "手E"}, None], "al": "notadict"},
            {"id": 4, "name": "歌F", "ar": [], "dt": "long", "fee": "free"},  # 非 int dt/fee → 0
        ]
        real, calls = _patch_urlopen(
            json.dumps({"code": 200, "data": {"dailySongs": raw}}).encode("utf-8"))
        try:
            songs = b.fetch_daily_songs(limit=10, force_refresh=True)
        finally:
            urllib.request.urlopen = real
        assert calls["n"] == 1
        assert [s["id"] for s in songs] == [1, 2, 3, 4]
        assert songs[0] == {
            "id": 1, "name": "歌A", "artists": "手A/手B", "album": "专A",
            "pic_url": "http://pic/1", "dt_ms": 200000, "fee": 0,
            "share_url": "https://music.163.com/song?id=1",
        }
        assert set(songs[0].keys()) == {"id", "name", "artists", "album", "pic_url", "dt_ms", "fee", "share_url"}
        assert songs[1]["artists"] == "未知"   # ar 非 list → 未知
        assert songs[1]["album"] == "专B"
        assert songs[2]["artists"] == "手E"    # ar 混合非 dict → 仅取 dict 条目
        assert songs[2]["album"] == "" and songs[2]["pic_url"] == ""  # al 非 dict → 空
        assert songs[3]["dt_ms"] == 0 and songs[3]["fee"] == 0
        assert saved == [songs]  # _save_cache 收到过滤后的结果
    print("  OK test_daily_songs_schema_filter")


def test_daily_songs_non_dict_resp():
    """v9 审计 F-3:fetch_daily_songs resp 非 dict(list 等)→ None(不抛 AttributeError,不写缓存)"""
    with tempfile.TemporaryDirectory() as td:
        b = _bridge(td)
        b._load_cookie = lambda: "MUSIC_U=test"
        b._load_cache = lambda: None  # 不读真实缓存文件
        saved = []
        b._save_cache = lambda songs: saved.append(songs)  # 不写真实缓存文件
        real, calls = _patch_urlopen(json.dumps([1, 2, 3]).encode("utf-8"))
        try:
            assert b.fetch_daily_songs(force_refresh=True) is None
        finally:
            urllib.request.urlopen = real
        assert calls["n"] == 1
        assert saved == []  # 失败不写缓存
    print("  OK test_daily_songs_non_dict_resp")



