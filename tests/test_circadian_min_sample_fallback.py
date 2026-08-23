#!/usr/bin/env python3
"""test_circadian_min_sample_fallback.py — 盲区5 circadian min_sample fallback（AUD-030）

Given: chiguo_circadian.CircadianTracker / estimate_sleep_window
When:  样本不足 / 样本恰好达标 / 周末桶折算
Then:  不足→None/保持旧值；达标→覆盖；周末桶门槛 2/7 折算可达
"""
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_circadian import CircadianTracker, estimate_sleep_window

CST = timezone(timedelta(hours=8))


def test_estimate_sample_insufficient_returns_none():
    """sample_days < min_sample → None（调用方回退默认窗口）。"""
    counts = [5 if 0 <= h < 8 else 0 for h in range(24)]
    assert estimate_sleep_window(counts, sample_days=3, history_days=14, min_sample_days=7) is None


def test_estimate_sample_exact_threshold():
    """sample_days == min_sample → 正常返回（边界可达）。"""
    counts = [5 if 0 <= h < 8 else 0 for h in range(24)]
    out = estimate_sleep_window(counts, sample_days=7, history_days=14, min_sample_days=7)
    assert out is not None
    assert 0 <= out["quiet_start"] <= 23


def test_recompute_insufficient_keeps_old_window():
    """recompute 时不满足 eff_min_sample 的桶保持旧值（不覆盖）。"""
    tr = CircadianTracker(
        weekday_quiet_start=1, weekday_quiet_end=6, weekday_confidence=0.9,
        weekend_quiet_start=2, weekend_quiet_end=7, weekend_confidence=0.8,
        quiet_start=1, quiet_end=6, confidence=0.9,
    )
    # 注入仅 1 天的 reply_days，且 history_days=14/min_sample=7 → weekday 需 7 天，不达标
    now = datetime(2026, 6, 15, 12, 0, tzinfo=CST)  # 周一
    tr.record(now, history_days=14, bucket="weekday")
    out = tr.recompute(min_sample_days=7, history_days=14)
    # weekday 样本不足 → 保持旧值
    assert tr.weekday_quiet_start == 1 and tr.weekday_quiet_end == 6
    assert out is None or out["quiet_start"] == 1  # 若 weekend 也不达标则整体 None


def test_weekend_ratio_scales_threshold():
    """周末桶 eff_min_sample = max(1, round(7*2/7))=2，2 天即可达标；weekday 仍需 7。"""
    tr = CircadianTracker()
    base = datetime(2026, 6, 13, 10, 0, tzinfo=CST)  # 周六
    # 注入 2 天 weekend 数据（周六+周日）
    tr.record(base, history_days=14, bucket="weekend")
    tr.record(base + timedelta(days=1), history_days=14, bucket="weekend")
    # 此时 weekday 无数据，weekend 2 天应可达标（折算后门槛 2）
    out = tr.recompute(min_sample_days=7, history_days=14)
    assert out is not None, "weekend 2 天应可达标（折算门槛 2）"
    # weekday 桶因 0 天不应覆盖
    assert tr.weekday_confidence == 0.0


def test_sample_days_cross_bucket_dedup():
    """sample_days 跨桶按日期去重：同一天 weekday+weekend 各一条 → 计 1 天。"""
    tr = CircadianTracker()
    d = datetime(2026, 6, 15, 10, 0, tzinfo=CST)
    # 同一天两桶各记一条（通过直接构造 reply_days）
    tr.reply_days = [
        {"date": "2026-06-15", "hours": [10], "bucket": "weekday"},
        {"date": "2026-06-15", "hours": [22], "bucket": "weekend"},
    ]
    tr.active_days = []
    tr.recompute(min_sample_days=1, history_days=14)
    assert tr.sample_days == 1, f"同日跨桶应去重为 1, got {tr.sample_days}"


def test_recompute_both_buckets_sample_days():
    """两桶各 1 天不同日期 → sample_days=2。"""
    tr = CircadianTracker()
    tr.reply_days = [
        {"date": "2026-06-15", "hours": [10], "bucket": "weekday"},
        {"date": "2026-06-14", "hours": [22], "bucket": "weekend"},
    ]
    tr.active_days = []
    tr.recompute(min_sample_days=1, history_days=14)
    assert tr.sample_days == 2


def test_e2e_fallback_chain():
    """E2E：estimate 返回 None 时 _sync_quiet_window 应回退配置默认（0,8）。"""
    counts = [0] * 24
    out = estimate_sleep_window(counts, sample_days=0, history_days=14, min_sample_days=7)
    assert out is None
    # 调用方回退语义由 chiguo_state._sync_quiet_window 承载，此处仅验证上游 None 契约
