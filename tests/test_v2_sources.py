"""tests/test_v2_sources.py — Chiguo v2 Phase 3「世界观测源」TDD。

sources 包只提供事实：Observation / observe_all 基座 + schedule/holiday/netease/weather
四个 Source 的 observe() 输出。Source 绝不决定 send / 触发。
"""
import json
import sys
from dataclasses import FrozenInstanceError, fields
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chiguo_time import CST  # noqa: E402
from schedule.day_plan import week_number  # noqa: E402
from schedule.holiday import HolidayParser  # noqa: E402
from schedule.override_store import OverrideStore  # noqa: E402
from sources.base import Observation, observe_all  # noqa: E402
from sources.holiday import HolidaySource  # noqa: E402
from sources.netease import NeteaseSource  # noqa: E402
from sources.schedule import ScheduleSource  # noqa: E402
from sources.weather import WeatherSource  # noqa: E402

SEMESTER_START = date(2026, 2, 23)


def _now(y=2026, mo=10, d=13, h=10, mi=5):
    return datetime(y, mo, d, h, mi, tzinfo=CST)


# ── base：Observation / observe_all ─────────────────────────────


def test_observation_fields_and_frozen():
    now = _now()
    o = Observation(type="weather.changed", source="weather", observed_at=now,
                    expires_at=None, payload={"a": 1})
    assert [f.name for f in fields(Observation)] == \
        ["type", "source", "observed_at", "expires_at", "payload"]
    assert o.payload == {"a": 1}
    with pytest.raises(FrozenInstanceError):
        o.type = "x"


class _GoodSource:
    name = "good"

    def observe(self, now):
        return [Observation("good.tick", "good", now, None, {})]


class _BoomSource:
    name = "boom"

    def observe(self, now):
        raise RuntimeError("数据面炸了")


class _NoneSource:
    name = "none_source"

    def observe(self, now):
        return None


def test_observe_all_isolates_single_source_failure(capsys):
    now = _now()
    result = observe_all([_GoodSource(), _BoomSource(), _NoneSource(), _GoodSource()], now)
    assert len(result) == 2                      # 坏源空结果，其余源照常
    err = capsys.readouterr().err
    assert "boom" in err and "RuntimeError" in err


def test_all_sources_expose_protocol(tmp_path):
    srcs = [
        ScheduleSource(str(tmp_path), {}),
        HolidaySource(str(tmp_path)),
        NeteaseSource(service=_FakeNetease([], enabled=False)),
        WeatherSource({}),
    ]
    assert [s.name for s in srcs] == ["schedule", "holiday", "netease", "weather"]
    assert all(callable(s.observe) for s in srcs)


# ── ScheduleSource ──────────────────────────────────────────────


def _fake_sources(tmp_path, now, courses, holiday=None, break_state=None):
    """手工 Sources 替身（monkeypatch load_sources 用），courses: {period: (name, location)}。"""
    wk = week_number(now.date(), SEMESTER_START)
    day = {p: {"course": name, "teacher": "", "location": loc, "weeks": {wk}}
           for p, (name, loc) in courses.items()}
    return SimpleNamespace(
        base_dir=str(tmp_path), semester_start=SEMESTER_START, semester_end=None,
        holiday=holiday or HolidayParser(str(tmp_path / "holidays.json")),
        anniversaries=None, overrides=OverrideStore(str(tmp_path)),
        break_state=break_state, schedule={now.date().weekday(): day} if day else {},
        schedule_valid=bool(day),
    )


def test_schedule_state_free_on_empty_data(tmp_path):
    now = _now(h=10, mi=5)
    src = ScheduleSource(str(tmp_path), {"schedule": {
        "semester_start": "2026-02-23", "semester_end": "2027-01-10"}})
    obs = src.observe(now)
    assert [o.type for o in obs] == ["schedule.state"]
    p = obs[0].payload
    assert p["in_class"] is False
    assert p["class_load"] == "free"
    assert p["remaining_classes"] == 0
    assert p["on_break"] is False and p["weekend"] is False and p["holiday"] is None
    assert p["current_course"] is None
    assert obs[0].expires_at == now + timedelta(minutes=30)


def test_schedule_state_in_class_and_course_starting(monkeypatch, tmp_path):
    now = _now(h=10, mi=25)                      # 第 3 节（10:00-10:45）中，距第 4 节 25 分钟
    fake = _fake_sources(tmp_path, now, {3: ("高等数学", "A101"), 4: ("英语", "B202")})
    monkeypatch.setattr("sources.schedule.load_sources", lambda *a, **k: fake)
    obs = ScheduleSource(str(tmp_path), {"schedule": {"semester_start": "2026-02-23"}}).observe(now)
    by_type = {o.type: o for o in obs}
    assert set(by_type) == {"schedule.state", "schedule.course_starting"}
    state = by_type["schedule.state"]
    assert state.source == "schedule" and state.observed_at == now
    p = state.payload
    assert p["in_class"] is True
    assert p["current_course"] == "高等数学"
    assert p["class_load"] == "light"
    assert p["remaining_classes"] == 1
    assert p["week_num"] == week_number(now.date(), SEMESTER_START)
    assert p["on_break"] is False and p["holiday"] is None and p["weekend"] is False
    start = by_type["schedule.course_starting"]
    assert start.expires_at == datetime(2026, 10, 13, 10, 50, tzinfo=CST)
    assert start.payload["period"] == 4
    assert start.payload["course"] == "英语"
    assert start.payload["minutes_until"] == 25


def test_schedule_state_holiday_suppresses_classes(monkeypatch, tmp_path):
    """节假日即使缓存里有当日课程也不呈现（对齐 facade 语义）。"""
    now = _now(mo=10, d=2, h=10, mi=25)          # 2026-10-02 国庆假期内（周五）
    (tmp_path / "holidays.json").write_text(json.dumps({
        "holidays": {"国庆节": {"start": "2026-10-01", "end": "2026-10-07"}},
    }, ensure_ascii=False))
    fake = _fake_sources(tmp_path, now, {3: ("高等数学", "A101")})
    monkeypatch.setattr("sources.schedule.load_sources", lambda *a, **k: fake)
    obs = ScheduleSource(str(tmp_path), {}).observe(now)
    assert [o.type for o in obs] == ["schedule.state"]     # 不产出 course_starting
    p = obs[0].payload
    assert p["holiday"] == "国庆节"
    assert p["in_class"] is False and p["class_load"] == "free" and p["remaining_classes"] == 0


def test_schedule_source_swallows_data_failure(monkeypatch, tmp_path, capsys):
    def boom(*a, **k):
        raise RuntimeError("课表数据面炸了")

    monkeypatch.setattr("sources.schedule.load_sources", boom)
    assert ScheduleSource(str(tmp_path), {}).observe(_now()) == []
    assert "schedule" in capsys.readouterr().err


# ── HolidaySource ───────────────────────────────────────────────


def test_holiday_upcoming(tmp_path):
    (tmp_path / "holidays.json").write_text(json.dumps({
        "holidays": {"测试节": {"start": "2026-06-03", "end": "2026-06-05"}},
        "makeup_workdays": {},
    }, ensure_ascii=False))
    obs = HolidaySource(str(tmp_path)).observe(_now(y=2026, mo=6, d=1, h=9, mi=0))
    upcoming = [o for o in obs if o.type == "holiday.upcoming"]
    assert len(upcoming) == 1
    assert upcoming[0].payload == {"name": "测试节", "start": "2026-06-03",
                                   "end": "2026-06-05", "days_until": 2}
    assert upcoming[0].source == "holiday"
    # 假期首日当天全程有效 → 过期 = 次日 00:00（H3：当天观测不得自我过期）
    assert upcoming[0].expires_at == datetime(2026, 6, 4, 0, 0, tzinfo=CST)


def test_holiday_today_observation_not_expired(tmp_path):
    """假期首日当天 observe → 观测必须仍在有效期内（否则 holiday 机会永不可达）。"""
    (tmp_path / "holidays.json").write_text(json.dumps({
        "holidays": {"测试节": {"start": "2026-06-03", "end": "2026-06-05"}},
        "makeup_workdays": {},
    }, ensure_ascii=False))
    now = _now(y=2026, mo=6, d=3, h=10, mi=0)
    obs = HolidaySource(str(tmp_path)).observe(now)
    today_obs = [o for o in obs if o.type == "holiday.upcoming" and o.payload["days_until"] == 0]
    assert len(today_obs) == 1
    assert today_obs[0].expires_at > now


def test_anniversary_today_observation_not_expired(tmp_path):
    (tmp_path / "anniversaries.json").write_text(json.dumps({
        "anniversaries": [{"id": "a1", "type": "anniversary", "name": "今天纪念", "date": "06-03"}],
    }, ensure_ascii=False))
    now = _now(y=2026, mo=6, d=3, h=10, mi=0)
    obs = HolidaySource(str(tmp_path)).observe(now)
    ann = [o for o in obs if o.type == "anniversary.upcoming"]
    assert len(ann) == 1 and ann[0].expires_at > now


def test_anniversary_upcoming_today_and_7d(tmp_path):
    (tmp_path / "anniversaries.json").write_text(json.dumps({
        "anniversaries": [
            {"id": "a1", "type": "anniversary", "name": "相遇纪念", "date": "06-03"},
            {"id": "a2", "type": "anniversary", "name": "今天纪念", "date": "06-01"},
        ]}, ensure_ascii=False))
    obs = HolidaySource(str(tmp_path)).observe(_now(y=2026, mo=6, d=1, h=9, mi=0))
    ann = {o.payload["name"]: o.payload for o in obs if o.type == "anniversary.upcoming"}
    assert ann["今天纪念"] == {"name": "今天纪念", "date": "2026-06-01", "days_until": 0}
    assert ann["相遇纪念"] == {"name": "相遇纪念", "date": "2026-06-03", "days_until": 2}


def test_holiday_source_empty_when_nothing_upcoming(tmp_path):
    # 2026-06-01 起 7 天内无内嵌假期开始，且无数据文件 → 空
    assert HolidaySource(str(tmp_path)).observe(_now(y=2026, mo=6, d=1, h=9, mi=0)) == []


def test_holiday_source_corrupt_files_do_not_crash(tmp_path):
    (tmp_path / "holidays.json").write_text("{not json")
    (tmp_path / "anniversaries.json").write_text("[1, 2, 3]")
    assert HolidaySource(str(tmp_path)).observe(_now(y=2026, mo=6, d=1, h=9, mi=0)) == []


# ── NeteaseSource ───────────────────────────────────────────────


class _FakeNetease:
    """鸭子类型替身：enabled 属性 + fetch_play_proof(now)。"""

    def __init__(self, plays, enabled=True):
        self.enabled = enabled
        self._plays = plays
        self.calls = 0

    def fetch_play_proof(self, now):
        self.calls += 1
        return self._plays


def test_music_observed_only_recent_2h():
    now = _now(y=2026, mo=6, d=1, h=23, mi=30)
    ms = int(now.timestamp() * 1000)
    plays = [
        {"playTime": ms - 3 * 3600 * 1000, "name": "老歌", "artist": "旧"},
        {"playTime": ms - 30 * 60 * 1000, "name": "新歌", "artist": "新", "extra": "丢弃"},
    ]
    svc = _FakeNetease(plays)
    obs = NeteaseSource(service=svc).observe(now)
    assert len(obs) == 1
    o = obs[0]
    assert o.type == "music.observed" and o.source == "netease"
    assert o.payload == {"plays": [{"playTime": ms - 30 * 60 * 1000,
                                    "name": "新歌", "artist": "新"}]}
    assert svc.calls == 1


def test_music_empty_when_no_recent_play():
    now = _now(y=2026, mo=6, d=1, h=23, mi=30)
    ms = int(now.timestamp() * 1000)
    old = [{"playTime": ms - 3 * 3600 * 1000, "name": "老歌", "artist": "旧"}]
    assert NeteaseSource(service=_FakeNetease(old)).observe(now) == []
    assert NeteaseSource(service=_FakeNetease([])).observe(now) == []
    assert NeteaseSource(service=_FakeNetease(None)).observe(now) == []


def test_music_disabled_source_not_called():
    now = _now(y=2026, mo=6, d=1, h=23, mi=30)
    svc = _FakeNetease([{"playTime": int(now.timestamp() * 1000), "name": "x", "artist": "y"}],
                       enabled=False)
    assert NeteaseSource(service=svc).observe(now) == []
    assert svc.calls == 0


def test_music_source_failure_returns_empty(capsys):
    class _BoomNetease:
        enabled = True

        def fetch_play_proof(self, now):
            raise RuntimeError("网易云炸了")

    assert NeteaseSource(service=_BoomNetease()).observe(_now(y=2026, mo=6, d=1, h=23)) == []
    assert "netease" in capsys.readouterr().err


def test_netease_lazy_service_disabled(tmp_path):
    src = NeteaseSource(base_dir=str(tmp_path), config={"netease": {"enabled": False}})
    assert src.observe(_now(y=2026, mo=6, d=1, h=23)) == []


# ── WeatherSource ───────────────────────────────────────────────


class _FakeHTTPResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_weather_disabled_returns_empty():
    now = _now(y=2026, mo=6, d=15, h=14)
    assert WeatherSource({}).observe(now) == []
    assert WeatherSource({"weather": {"enabled": True}}).observe(now) == []   # 无端点


def test_weather_open_meteo_payload(monkeypatch):
    import sources.weather as weather_mod
    captured = {}

    def fake_urlopen(url, timeout=None):
        captured["url"], captured["timeout"] = url, timeout
        return _FakeHTTPResponse({"current": {
            "time": "2026-06-15T14:00", "temperature_2m": 24.5, "weather_code": 61}})

    monkeypatch.setattr(weather_mod.urllib_request, "urlopen", fake_urlopen)
    now = _now(y=2026, mo=6, d=15, h=14)
    obs = WeatherSource({"weather": {
        "enabled": True, "base_url": "https://api.example.com/v1/forecast",
        "latitude": 30.25, "longitude": 120.1, "timeout_seconds": 3}}).observe(now)
    assert len(obs) == 1
    o = obs[0]
    assert o.type == "weather.changed" and o.source == "weather"
    assert o.payload["condition"] == "小雨"
    assert o.payload["temperature"] == 24.5
    assert o.payload["weather_code"] == 61
    assert o.expires_at == now + timedelta(minutes=30)
    assert "latitude=30.25" in captured["url"] and "longitude=120.1" in captured["url"]
    assert "current=temperature_2m" in captured["url"]
    assert captured["timeout"] == 3.0


def test_weather_network_failure_returns_empty(monkeypatch, capsys):
    import sources.weather as weather_mod

    def fake_urlopen(url, timeout=None):
        raise OSError("网络不可达")

    monkeypatch.setattr(weather_mod.urllib_request, "urlopen", fake_urlopen)
    obs = WeatherSource({"weather": {"enabled": True,
                                     "base_url": "https://api.example.com/x"}}).observe(_now())
    assert obs == []
    assert "weather" in capsys.readouterr().err
