#!/usr/bin/env python3
"""test_trigger_comfort_matrix.py — 盲区3 comfort 三维矩阵（AUD-030）

Given: mood low/distressed × affection 50/80 × intensity 0.3/0.8 × TTL fresh/stale
When:  evaluate_triggers 收集 emotion 候选
Then:  comfort 权重按 affection/intensity 线性放缩，TTL 过期与 mood=neutral 时不出现
"""
import os
import sys
import random
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState
from chiguo_trigger import _collect_emotion_candidates
from chiguo_math import mood_fresh


def _make_state(tmp: str, now: datetime) -> ChiguoState:
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    s = ChiguoState(cfg)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    return s


def _collect_comfort(s, now, mood: dict, ttl_min: float = 360.0):
    """构造 fresh mood 并收集 comfort 候选；返回 comfort weight 或 None。"""
    # 确保 mood_fresh：mood.at 缺省则用 now
    m = dict(mood)
    if "at" not in m:
        m["at"] = now.isoformat()
    s.cooldown.user_mood = m
    trg_cfg = dict(s.config.get("trigger", {}))
    trg_cfg["user_mood_ttl_minutes"] = ttl_min
    # 保证 comfort 门控可用
    trg_cfg["comfort_weight_base"] = trg_cfg.get("comfort_weight_base", 0.6) or 0.6
    trg_cfg["comfort_baseline"] = 0.5
    trg_cfg["comfort_min_weight"] = 0.02
    s.config["trigger"] = trg_cfg
    # silent_h 任意非阻塞值
    cands = _collect_emotion_candidates(s, now, trg_cfg, silent_h=10)
    comfort = [c for c in cands if c["trigger"].type == "comfort"]
    if not comfort:
        return None
    return comfort[0]["weight"]


def test_comfort_appears_when_enabled():
    """mood low + fresh + comfort_weight_base>0 → comfort 候选出现。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        w = _collect_comfort(s, now, {"mood": "low", "intensity": 0.7})
        assert w is not None, "low+fresh 应出现 comfort"
        assert w > 0


def test_comfort_scales_with_affection():
    """affection 越高，comfort 权重越大（1 + (aff-50)/100 线性）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s_low = _make_state(td + "_1", now) if False else None
        # 用两个独立 state 避免缓存干扰
        with tempfile.TemporaryDirectory() as td2:
            # 复用同一 tmp 隔离：直接改 affection
            s = _make_state(td, now)
            s.emotion.affection = 50
            w50 = _collect_comfort(s, now, {"mood": "low", "intensity": 0.6})
            s.emotion.affection = 80
            w80 = _collect_comfort(s, now, {"mood": "low", "intensity": 0.6})
            assert w50 is not None and w80 is not None
            assert w80 > w50, f"affection 80 权重应 >50: {w80} vs {w50}"


def test_comfort_scales_with_intensity():
    """intensity 越高，comfort raw_cf 越大。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.emotion.affection = 60
        w_low = _collect_comfort(s, now, {"mood": "low", "intensity": 0.2})
        w_high = _collect_comfort(s, now, {"mood": "low", "intensity": 0.9})
        assert w_low is not None and w_high is not None
        assert w_high > w_low, f"intensity 0.9 应 >0.2: {w_high} vs {w_low}"


def test_comfort_distressed_also_triggers():
    """mood distressed 同样触发 comfort（与 low 并列）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        w = _collect_comfort(s, now, {"mood": "distressed", "intensity": 0.6})
        assert w is not None, "distressed 应触发 comfort"


def test_comfort_neutral_no_trigger():
    """mood neutral → 不触发 comfort。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        w = _collect_comfort(s, now, {"mood": "neutral", "intensity": 0.9})
        assert w is None, f"neutral 不应触发 comfort, got {w}"


def test_comfort_ttl_expiry_removes():
    """TTL 过期 → mood_fresh False → comfort 消失。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        stale_at = (now - timedelta(minutes=400)).isoformat()
        w = _collect_comfort(s, now, {"mood": "low", "intensity": 0.8, "at": stale_at}, ttl_min=360.0)
        assert w is None, f"TTL 过期不应触发 comfort, got {w}"


def test_comfort_weight_zero_disabled():
    """comfort_weight_base=0 → comfort 恒不出现（默认关闭）。"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        s.cooldown.user_mood = {"mood": "low", "intensity": 0.9, "at": now.isoformat()}
        trg_cfg = dict(s.config.get("trigger", {}))
        trg_cfg["comfort_weight_base"] = 0.0
        s.config["trigger"] = trg_cfg
        cands = _collect_emotion_candidates(s, now, trg_cfg, silent_h=10)
        assert not any(c["trigger"].type == "comfort" for c in cands)
