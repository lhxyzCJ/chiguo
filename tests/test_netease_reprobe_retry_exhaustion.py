#!/usr/bin/env python3
"""test_netease_reprobe_retry_exhaustion.py — 盲区7 netease reprobe/retry 串联（AUD-031）

Given: netease/service.NeteaseService(retry_count/backoff/reprobe_minutes) + bridge retry 语义
When:  retry 全败→faulty→peek 走 fault 话题；reprobe 到期后 refresh_health 恢复；enabled=False 时全路径 None
Then:  时序串联均被覆盖
"""
import sys
import os
import tempfile
import json
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from netease.service import NeteaseService
from netease.bridge import NeteaseBridge

CST = timezone(timedelta(hours=8))


def _now():
    return datetime(2026, 6, 15, 14, 0, tzinfo=CST)


def test_retry_exhausted_marks_faulty_no_probe():
    """bridge fetch 全败 → service 置 faulty；下次 peek 未到 reprobe 前不探针、走 fault 话题。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = {"netease": {"retry_count": 1, "retry_backoff_seconds": 0, "reprobe_minutes": 9999},
               "topic_picker": {}}
        svc = NeteaseService(cfg, str(base))
        # 模拟 bridge 全败
        svc.bridge.fetch_daily_songs = MagicMock(return_value=[])
        svc.bridge.fetch_recent_play = MagicMock(return_value=[])
        svc.bridge.check_health = MagicMock(return_value={"api_alive": False})
        # 两源全失败 → refresh_health 置 faulty
        out = svc._pick_and_fetch(_now(), consume=False)
        assert out is None
        assert svc._health["faulty"] is True
        # 下次 peek：faulty 且未到 reprobe → 不探针，直接 fault 话题（不消费 music 配额）
        with patch.object(svc, "refresh_health") as mock_refresh:
            topic = svc.peek_music_topic(_now())
            mock_refresh.assert_not_called()
            assert topic is not None and topic["type"] == "netease_fault"


def test_reprobe_after_interval_refreshes_health():
    """reprobe 到期后 peek 会 refresh_health；若恢复则走正常话题。"""
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        cfg = {"netease": {"retry_count": 1, "retry_backoff_seconds": 0, "reprobe_minutes": 0.05},
               "topic_picker": {}}
        svc = NeteaseService(cfg, str(base))
        svc._health["faulty"] = True
        svc._health["last_check"] = (_now() - timedelta(minutes=1)).isoformat()
        svc._health["failure_reason"] = "unreachable"
        # 模拟恢复
        svc.bridge.check_health = MagicMock(return_value={"api_alive": True, "logged_in": True})
        svc.bridge.fetch_daily_songs = MagicMock(return_value=[{"name": "SongA", "artists": "Art"}])
        # peek 触发 reprobe → 恢复 → 选中 daily
        topic = svc.peek_music_topic(_now())
        assert topic is not None
        assert topic["type"] == "netease_music"
        assert svc._health["faulty"] is False


def test_fetch_play_proof_disabled_returns_none():
    """enabled=False 时 fetch_play_proof 与 peek 均直接 None（不探针不消费）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {"enabled": False}, "topic_picker": {}}
        svc = NeteaseService(cfg, str(td))
        assert svc.fetch_play_proof(_now()) is None
        assert svc.peek_music_topic(_now()) is None


def test_fetch_play_proof_naive_now_cst():
    """naive now → 内部补 CST 后仍可 fetch（不抛）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {"enabled": True, "reprobe_minutes": 9999}, "topic_picker": {}}
        svc = NeteaseService(cfg, str(td))
        svc.bridge.fetch_recent_play = MagicMock(return_value=[{"playTime": int(_now().timestamp() * 1000), "name": "X"}])
        svc.bridge.check_health = MagicMock(return_value={"api_alive": True, "logged_in": True})
        naive = datetime(2026, 6, 15, 14, 0)  # naive
        out = svc.fetch_play_proof(naive)
        assert isinstance(out, list)


def test_music_quota_exhausted_returns_none():
    """music 配额用尽 → peek 返回 None（不尝试拉取）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {}, "topic_picker": {"netease_daily_quota": 1}}
        svc = NeteaseService(cfg, str(td))
        svc._health["quota_music_used"] = 1
        svc._health["quota_music_day"] = _now().strftime("%Y-%m-%d")
        svc.bridge.fetch_daily_songs = MagicMock(return_value=[{"name": "X", "artists": "Y"}])
        assert svc.peek_music_topic(_now()) is None


def test_in_class_or_quiet_returns_none():
    """上课中或静默窗口 → peek 直接 None（时段门禁优先于故障分支）。"""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {}, "topic_picker": {}}
        svc = NeteaseService(cfg, str(td))
        svc._health["faulty"] = True
        assert svc.peek_music_topic(_now(), in_class=True) is None
        assert svc.peek_music_topic(_now(), in_quiet_window=True) is None
