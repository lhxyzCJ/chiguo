#!/usr/bin/env python3
"""test_circadian.py — chiguo_circadian 生物钟学习单元测试（v8）"""

import json
import os
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_circadian import (
    estimate_sleep_window, track_reply_hour, aggregate_hours,
    count_sample_days, bucket_for, CircadianTracker,
)
from chiguo_state import ChiguoState


def test_cold_start_returns_none():
    """无数据(0 天)或数据不足(< min_sample_days) → None(回退默认窗口)"""
    counts = [0] * 24
    assert estimate_sleep_window(counts, 0, 14) is None
    assert estimate_sleep_window(counts, 5, 14) is None
    assert estimate_sleep_window(counts, 14, 14, min_sample_days=7) is None
    print("  OK test_cold_start_returns_none")


def test_clear_night_sleep_window():
    """0-5 点零回复,其余每小时 10 条 → 学习窗口 0-5(最小宽度优先)"""
    counts = [0] * 6 + [10] * 18
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["quiet_start"] == 0
    assert est["quiet_end"] == 5  # 不含 end,与 cooldown 窗口语义一致
    assert est["width"] == 5
    assert est["confidence"] == 1.0
    print("  OK test_clear_night_sleep_window")


def test_wrap_midnight_window():
    """22,23,0,1,2 点零回复 → 跨午夜窗口 22-3(qe < qs,端不含)"""
    counts = [0] * 3 + [10] * 19 + [0] * 2  # 0,1,2 点零;22,23 点零
    counts[22] = 0  # 22 点零
    counts[23] = 0  # 23 点零
    # 零回复小时:22,23,0,1,2 → 唯一 sum=0 的宽5窗口:start=22
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["quiet_start"] == 22
    assert est["quiet_end"] == 3  # (22+5) % 24 = 3,窗口含 22,23,0,1,2
    assert est["width"] == 5
    print("  OK test_wrap_midnight_window")


def test_partial_data_confidence_scales():
    """样本天数不足满窗口 → 置信度 = 完整度 × 安静度,低于 1"""
    counts = [0] * 6 + [10] * 18
    est = estimate_sleep_window(counts, 10, 14)
    assert est is not None
    assert est["confidence"] == 0.714
    print("  OK test_partial_data_confidence_scales")


def test_window_with_activity_lowers_confidence():
    """窗口内有零星活动 → 安静度 < 1,置信度下调但仍可通过阈值"""
    counts = [2] * 8 + [10] * 16  # 0-7 点每小时 2 条
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["width"] == 5
    assert est["quiet_start"] == 0
    assert 0.6 < est["confidence"] < 1.0
    print("  OK test_window_with_activity_lowers_confidence")


def test_invalid_inputs():
    """长度非 24 / 总回复为 0 → None,不崩溃"""
    assert estimate_sleep_window([1, 2, 3], 14, 14) is None
    assert estimate_sleep_window([0] * 24, 14, 14) is None
    print("  OK test_invalid_inputs")


def test_track_reply_hour_append_prune_aggregate():
    """track_reply_hour: 同日追加、跨日新开、过期修剪;aggregate/count 正确"""
    now = datetime(2026, 7, 31, 14, 30, tzinfo=CST)
    days = []
    days = track_reply_hour(days, now, history_days=3, bucket="weekday")
    days = track_reply_hour(days, now, history_days=3, bucket="weekday")
    assert len(days) == 1 and days[0]["hours"] == [14, 14]
    days = track_reply_hour(days, now.replace(day=30, hour=13), history_days=3, bucket="weekday")
    assert len(days) == 2
    days = track_reply_hour(days, now.replace(day=28), history_days=3, bucket="weekday")
    # 3 天窗口:7-28 起修剪 → 只剩 7-30、7-31
    assert len(days) == 2, days
    hours = aggregate_hours(days)
    assert len(hours) == 24 and hours[14] == 2 and hours[13] == 1
    assert count_sample_days(days) == 2
    print("  OK test_track_reply_hour_append_prune_aggregate")


def test_tracker_recompute_defaults():
    """CircadianTracker.recompute: 数据不足 → 保持默认 0-8 / confidence 0"""
    tr = CircadianTracker()
    assert tr.recompute() is None
    assert tr.quiet_start == 0 and tr.quiet_end == 8 and tr.confidence == 0.0
    tr.reply_days = [{"date": "2026-07-31", "hours": list(range(9, 23)),
                      "bucket": "weekday"}]
    tr.recompute(min_sample_days=7, history_days=14)
    assert tr.sample_days == 1
    assert tr.confidence == 0.0  # 未达标不写窗口
    print("  OK test_tracker_recompute_defaults")


def test_tracker_recompute_success():
    """CircadianTracker.recompute: 满 14 天数据 → weekday 桶学出 0-5 睡眠窗口,置信度 1.0"""
    tr = CircadianTracker()
    tr.reply_days = [
        {"date": f"2026-07-{d:02d}", "hours": list(range(9, 24)),
         "bucket": "weekday"}
        for d in range(18, 32)  # 07-18..07-31,共 14 天
    ]
    est = tr.recompute(history_days=14)
    assert est is not None
    assert tr.sample_days == 14
    assert tr.weekday_quiet_start == 0
    assert tr.weekday_quiet_end == 5
    assert tr.weekday_confidence == 1.0
    print("  OK test_tracker_recompute_success")


def test_tracker_record_roundtrip():
    """CircadianTracker.record: 同日两次记录 → hours 累积,单日一条"""
    tr = CircadianTracker()
    now = datetime(2026, 7, 31, 14, 30, tzinfo=CST)
    tr.record(now, history_days=3)
    tr.record(now, history_days=3)
    assert len(tr.reply_days) == 1
    assert tr.reply_days[0]["hours"] == [14, 14]
    print("  OK test_tracker_record_roundtrip")


def test_tie_break_smallest_width():
    """全天均等计数 → (sum, width, start) 字典序:最小宽度、最靠前窗口胜"""
    counts = [5] * 24
    est = estimate_sleep_window(counts, 14, 14)
    assert est is not None
    assert est["width"] == 5
    assert est["quiet_start"] == 0
    print("  OK test_tie_break_smallest_width")


def test_corrupt_state_does_not_crash():
    """损坏记录(非法日期/缺 hours/hours 非列表)不崩溃;非法日期回退按 now 修剪"""
    now = datetime(2026, 7, 31, 14, 30, tzinfo=CST)
    days = [{"date": "garbage", "hours": [1]},
            {"date": "2026-07-31"},
            {"date": "2026-07-30", "hours": 5},
            {"date": "2026-07-27", "hours": [3]}]
    days = track_reply_hour(days, now, history_days=3, bucket="weekday")
    assert any(d.get("date") == "2026-07-31" and d.get("hours") == [14]
               for d in days)
    assert not any(d["date"] == "2026-07-27" for d in days)  # 过期仍被修剪
    hours = aggregate_hours(days)
    assert len(hours) == 24 and hours[14] == 1
    print("  OK test_corrupt_state_does_not_crash")


# ── v7 集成测试:state 持久化 + 动态静默窗口 ──────────────────


def _make_state(tmp: str) -> ChiguoState:
    """真实 toml 配置 + 临时目录锚定;mem0 指向不存在路径(确定性)"""
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    return ChiguoState(cfg)


def test_state_records_and_persists_circadian():
    """on_user_message → reply_days 追加(同桶同日合并);save/load 往返不丢失"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 7, 31, 14, 0, tzinfo=CST)  # 周五 14:00 → weekday
        s = _make_state(td)
        s.on_user_message(now)
        s.on_user_message(now.replace(hour=16))
        assert s.circadian.reply_days[0]["hours"] == [14, 16]
        assert s.circadian.reply_days[0]["bucket"] == "weekday"
        s.save(_backup=False, _increment_tick=False)

        s2 = _make_state(td)  # 重新加载
        assert s2.circadian.reply_days == [{"date": "2026-07-31",
                                            "hours": [14, 16],
                                            "bucket": "weekday"}]
    print("  OK test_state_records_and_persists_circadian")


def test_quiet_window_uses_learned_window_when_confident():
    """置信度高 → cooldown 睡眠窗口 = 学习窗口(替代固定 0-8)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        # 注入 14 天、夜间零回复的数据 → 置信度 1.0
        s.circadian.reply_days = [
            {"date": f"2026-07-{d:02d}", "hours": list(range(9, 24)),
             "bucket": "weekday"}
            for d in range(18, 32)
        ]
        s.circadian.recompute(history_days=14)
        assert s.circadian.weekday_confidence == 1.0
        # v8:兼容快照由 set_active_bucket 同步(Task 2 的 _sync_quiet_window 内部完成)
        s.circadian.set_active_bucket("weekday",
                                      *s.circadian.bucket_window("weekday"))
        assert s.circadian.confidence == 1.0
        # v8:按当前时刻分桶选窗 —— 显式传周一(weekday 桶),不依赖系统时钟
        s._sync_quiet_window(datetime(2026, 7, 27, 12, 0, tzinfo=CST))
        assert s.cooldown._quiet_start == 0  # 0-8 零回复 → 窗口 0-5
        assert s.cooldown._quiet_end == 5
        # 睡眠时间不算真沉默:10h 墙钟沉默,仅 [4:00,5:00) 1h 落入学习窗口 → silent≈9
        s.cooldown.last_user_message_at = (
            datetime(2026, 7, 31, 4, 0, tzinfo=CST).isoformat()
        )
        now = datetime(2026, 7, 31, 14, 0, tzinfo=CST)
        sil = s.cooldown.silent_hours(now)
        assert 8.5 < sil < 9.5, sil
    print("  OK test_quiet_window_uses_learned_window_when_confident")


def test_quiet_window_falls_back_when_not_confident():
    """置信度不足(冷启动) → 回退配置默认窗口 0-8,行为与 v6 完全一致"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s._sync_quiet_window(datetime(2026, 7, 27, 12, 0, tzinfo=CST))
        assert s.cooldown._quiet_start == 0
        assert s.cooldown._quiet_end == 8
    print("  OK test_quiet_window_falls_back_when_not_confident")


def test_state_old_version_without_circadian_loads():
    """v6 及更早的 state 文件(无 circadian 字段) → 默认值,不崩溃"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        payload = {"_version": 6, "emotion": {}, "cooldown": {}, "last_tick": "2026-07-31T10:00:00+08:00"}
        (s.state_path).write_text(json.dumps(payload))
        s2 = _make_state(td)
        assert s2.circadian.reply_days == []
        assert s2.circadian.quiet_start == 0 and s2.circadian.quiet_end == 8
        assert s2.cooldown._quiet_start == 0 and s2.cooldown._quiet_end == 8
    print("  OK test_state_old_version_without_circadian_loads")


def test_can_send_respects_learned_window():
    """学习窗口 [4,9) 置信 1.0 → 8:00 can_send False(睡眠中),10:00 放行"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        # 4-8 点零回复(其余时段有回复)→ 学习窗口 [4,9),置信 1.0
        s.circadian.reply_days = [
            {"date": f"2026-07-{d:02d}",
             "hours": list(range(4)) + list(range(9, 24)),
             "bucket": "weekday"}
            for d in range(18, 32)
        ]
        s.circadian.recompute(history_days=14)
        s.circadian.set_active_bucket("weekday",
                                      *s.circadian.bucket_window("weekday"))
        s._sync_quiet_window(datetime(2026, 7, 27, 12, 0, tzinfo=CST))
        assert s.cooldown._quiet_start == 4 and s.cooldown._quiet_end == 9
        # 8:00 在学习窗口内 → 禁止
        now8 = datetime(2026, 7, 31, 8, 0, tzinfo=CST)
        s.cooldown.last_user_message_at = (now8 - timedelta(hours=2)).isoformat()
        s.cooldown.current_date = now8.strftime("%Y-%m-%d")
        assert s.can_send(now8) is False
        # 8:30 仍在学习窗口内(但在旧固定窗口 0-8 之外)→ 判别审计修复的关键断言
        now830 = datetime(2026, 7, 31, 8, 30, tzinfo=CST)
        s.cooldown.last_user_message_at = (now830 - timedelta(hours=2)).isoformat()
        s.cooldown.current_date = now830.strftime("%Y-%m-%d")
        assert s.can_send(now830) is False
        # 10:00 窗口外 → 放行(其余条件满足)
        now10 = datetime(2026, 7, 31, 10, 0, tzinfo=CST)
        s.cooldown.last_user_message_at = (now10 - timedelta(hours=2)).isoformat()
        s.cooldown.current_date = now10.strftime("%Y-%m-%d")
        assert s.can_send(now10) is True
    print("  OK test_can_send_respects_learned_window")


def test_min_confidence_activates_at_min_sample_days():
    """7 天数据 + 完美安静 → confidence 0.5 ≥ min_confidence 0.5 → 学习窗口生效"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s.circadian.reply_days = [
            {"date": f"2026-07-{d:02d}", "hours": list(range(10, 24)),
             "bucket": "weekday"}
            for d in range(25, 32)
        ]
        s.circadian.recompute(history_days=14)
        assert s.circadian.weekday_confidence == 0.5
        s.circadian.set_active_bucket("weekday",
                                      *s.circadian.bucket_window("weekday"))
        assert s.circadian.confidence == 0.5
        s._sync_quiet_window(datetime(2026, 7, 27, 12, 0, tzinfo=CST))
        assert s.cooldown._quiet_start == 0 and s.cooldown._quiet_end == 5
    print("  OK test_min_confidence_activates_at_min_sample_days")


# ── v8 双作息测试:分桶纯函数 + 双桶独立学习 ──────────────────


def test_bucket_for():
    """分桶:调休优先 → 节假日 → 周五 20:00 起 / 周六全天 / 周日 20:00 前 → weekend"""
    no_holiday = lambda d: False
    no_makeup = lambda d: False
    fri = datetime(2026, 7, 31, 12, 0, tzinfo=CST)  # 周五
    assert fri.weekday() == 4
    assert bucket_for(fri.replace(hour=19, minute=59), no_holiday, no_makeup) == "weekday"
    assert bucket_for(fri.replace(hour=20, minute=0), no_holiday, no_makeup) == "weekend"
    sat = datetime(2026, 8, 1, 0, 0, tzinfo=CST)  # 周六
    assert sat.weekday() == 5
    assert bucket_for(sat, no_holiday, no_makeup) == "weekend"
    assert bucket_for(sat.replace(hour=23, minute=59), no_holiday, no_makeup) == "weekend"
    sun = datetime(2026, 8, 2, 19, 59, tzinfo=CST)  # 周日
    assert sun.weekday() == 6
    assert bucket_for(sun, no_holiday, no_makeup) == "weekend"
    assert bucket_for(sun.replace(hour=20, minute=0), no_holiday, no_makeup) == "weekday"
    mon = datetime(2026, 8, 3, 10, 0, tzinfo=CST)  # 周一
    assert mon.weekday() == 0
    assert bucket_for(mon, no_holiday, no_makeup) == "weekday"
    # 调休优先:周六上班 → weekday(即使也是节假日)
    assert bucket_for(sat, lambda d: True, lambda d: True) == "weekday"
    assert bucket_for(sat, no_holiday, lambda d: True) == "weekday"
    # 节假日(非调休):周三假期 → weekend;假日周五 → weekend
    wed = datetime(2026, 8, 5, 10, 0, tzinfo=CST)  # 周三
    assert wed.weekday() == 2
    assert bucket_for(wed, lambda d: True, no_makeup) == "weekend"
    assert bucket_for(fri, lambda d: True, no_makeup) == "weekend"
    print("  OK test_bucket_for")


def test_track_reply_hour_dual_bucket():
    """同日同桶合并;周五 20:00 前后 → 同日两条独立条目(不同桶);修剪不变"""
    fri = datetime(2026, 7, 31, 19, 0, tzinfo=CST)  # 周五
    days = []
    days = track_reply_hour(days, fri, history_days=14, bucket="weekday")
    days = track_reply_hour(days, fri.replace(hour=14), history_days=14, bucket="weekday")
    assert len(days) == 1
    assert days[0]["hours"] == [19, 14] and days[0]["bucket"] == "weekday"
    # 周五 20:00 后 → weekend 桶,同日不同桶 → 分开条目
    days = track_reply_hour(days, fri.replace(hour=21), history_days=14, bucket="weekend")
    assert len(days) == 2, days
    assert sorted(d["bucket"] for d in days) == ["weekday", "weekend"]
    wd = [d for d in days if d["bucket"] == "weekday"][0]
    we = [d for d in days if d["bucket"] == "weekend"][0]
    assert wd["hours"] == [19, 14] and we["hours"] == [21]
    # 同桶再记录 → 合并进对应条目
    days = track_reply_hour(days, fri.replace(hour=22), history_days=14, bucket="weekend")
    assert len(days) == 2
    assert [d for d in days if d["bucket"] == "weekend"][0]["hours"] == [21, 22]
    # 修剪不变:按最晚日期排他 > cutoff,跨桶条目一起保留
    later = datetime(2026, 8, 1, 10, 0, tzinfo=CST)
    days = track_reply_hour(days, later, history_days=3, bucket="weekend")
    assert all(d["date"] == "2026-07-31" or d["date"] == "2026-08-01"
               for d in days)
    assert len(days) == 3
    print("  OK test_track_reply_hour_dual_bucket")


def test_aggregate_count_bucket_filter():
    """aggregate_hours/count_sample_days 按桶过滤;bucket=None 全部计入"""
    days = [
        {"date": "2026-07-31", "hours": [10, 11], "bucket": "weekday"},
        {"date": "2026-08-01", "hours": [20], "bucket": "weekend"},
    ]
    all_h = aggregate_hours(days)
    assert all_h[10] == 1 and all_h[11] == 1 and all_h[20] == 1
    wd_h = aggregate_hours(days, "weekday")
    assert wd_h[10] == 1 and wd_h[11] == 1 and wd_h[20] == 0
    we_h = aggregate_hours(days, "weekend")
    assert we_h[20] == 1 and we_h[10] == 0
    assert count_sample_days(days) == 2
    assert count_sample_days(days, "weekday") == 1
    assert count_sample_days(days, "weekend") == 1
    print("  OK test_aggregate_count_bucket_filter")


def test_no_bucket_entries_excluded_when_filtering():
    """无 bucket 条目(迁移前旧格式)→ 过滤时视为不属于任何桶;bucket=None 仍计入"""
    days = [{"date": "2026-07-31", "hours": [8]},
            {"date": "2026-08-01", "hours": [9], "bucket": "weekday"}]
    assert aggregate_hours(days, "weekday")[8] == 0
    assert aggregate_hours(days, "weekday")[9] == 1
    assert aggregate_hours(days)[8] == 1 and aggregate_hours(days)[9] == 1
    assert count_sample_days(days) == 2
    assert count_sample_days(days, "weekday") == 1
    print("  OK test_no_bucket_entries_excluded_when_filtering")


def test_recompute_dual_bucket_independent():
    """weekday 桶 14 天 → 窗口 0-5 置信 1.0;weekend 桶仅 1 天 → 保持默认不被覆盖;sample_days 跨桶去重"""
    tr = CircadianTracker()
    tr.reply_days = [
        {"date": f"2026-07-{d:02d}", "hours": list(range(9, 24)),
         "bucket": "weekday"}
        for d in range(18, 32)  # 14 天工作日数据
    ]
    tr.reply_days.append({"date": "2026-08-01", "hours": list(range(9, 24)),
                          "bucket": "weekend"})
    est = tr.recompute(history_days=14)
    assert est is not None and est["quiet_start"] == 0
    assert tr.weekday_quiet_start == 0 and tr.weekday_quiet_end == 5
    assert tr.weekday_confidence == 1.0
    # weekend 数据不足 → 不覆盖,保持默认
    assert tr.weekend_quiet_start == 0 and tr.weekend_quiet_end == 8
    assert tr.weekend_confidence == 0.0
    assert tr.sample_days == 15  # 14 + 1,跨桶按日期去重
    print("  OK test_recompute_dual_bucket_independent")


def test_record_active_merges_into_bucket_counts():
    """合并计数:weekend 桶 1 天 reply 不激活;+6 天 active → 7 天达标激活(置信 0.5)"""
    tr = CircadianTracker()
    tr.record(datetime(2026, 8, 2, 12, 0, tzinfo=CST), history_days=14, bucket="weekend")
    assert tr.recompute(history_days=14) is None  # 仅 1 天 → 稀疏回退
    assert tr.weekend_confidence == 0.0
    for d in (25, 26, 27, 28, 29, 30):  # 6 天听歌活跃,不同日期
        tr.record_active(datetime(2026, 7, d, 12, 0, tzinfo=CST),
                         history_days=14, bucket="weekend")
    assert len(tr.active_days) == 6
    assert all(a["bucket"] == "weekend" for a in tr.active_days)
    est = tr.recompute(history_days=14)
    assert est is not None
    assert tr.weekend_quiet_start == 0 and tr.weekend_quiet_end == 5
    # v1.11+R5: 完整度按周末有效窗口(14×2/7≈4 天)计 → 7 天满覆盖,置信度 1.0
    # (修复前 completeness=7/14=0.5,周末桶结构不可达)
    assert tr.weekend_confidence == 1.0
    assert tr.sample_days == 7  # reply+active 按日期去重,同日期算 1 天
    # 合并计数生效:reply 1 条 + active 6 条(逐小时相加)
    assert aggregate_hours(tr.reply_days, "weekend")[12] == 1
    assert aggregate_hours(tr.active_days, "weekend")[12] == 6
    # record_active 损坏防护:非列表 → 重置
    tr.active_days = "corrupt"
    tr.record_active(datetime(2026, 7, 24, 12, 0, tzinfo=CST),
                     history_days=14, bucket="weekend")
    assert isinstance(tr.active_days, list) and len(tr.active_days) == 1
    print("  OK test_record_active_merges_into_bucket_counts")


def test_weekend_bucket_reachable_after_proportional_threshold():
    """v1.11+R5: 周末桶 4 天数据(14 天窗口内可达上限)即可激活——修复前
    min_sample_days=7 且 completeness=4/14<0.5 双重否决,周末桶睡眠窗口恒学不到。"""
    tr = CircadianTracker()
    tr.reply_days = [
        {"date": f"2026-08-{d:02d}", "hours": list(range(10, 24)),
         "bucket": "weekend"}
        for d in (1, 2, 8, 9)  # 两个周末共 4 天(2026-08-01/02 周六日,08/09 周六日)
    ]
    est = tr.recompute(history_days=14)
    assert est is not None
    assert tr.weekend_quiet_start == 0
    assert tr.weekend_quiet_end == 5
    # 4/4 完整度(周末有效窗口=round(14×2/7)=4)× 1.0 安静度 → 置信 1.0 ≥ 0.5 过门
    assert tr.weekend_confidence == 1.0
    # 不足 2 天 → 仍回退(有效门槛 max(1, round(7×2/7))=2)
    tr2 = CircadianTracker()
    tr2.reply_days = [{"date": "2026-08-01", "hours": list(range(10, 24)),
                       "bucket": "weekend"}]
    assert tr2.recompute(history_days=14) is None
    assert tr2.weekend_confidence == 0.0
    print("  OK test_weekend_bucket_reachable_after_proportional_threshold")


def test_bucket_window_and_set_active_bucket():
    """bucket_window 返回该桶窗口;set_active_bucket 把该桶窗口同步到兼容字段(往返);未知桶 → 默认 (0, 8, 0.0)"""
    tr = CircadianTracker()
    assert tr.bucket_window("weekday") == (0, 8, 0.0)
    assert tr.bucket_window("weekend") == (0, 8, 0.0)
    assert tr.bucket_window("unknown") == (0, 8, 0.0)
    # 往返:桶窗口 → bucket_window 读出 → set_active_bucket 同步兼容字段
    tr.weekday_quiet_start, tr.weekday_quiet_end, tr.weekday_confidence = 2, 6, 0.8
    tr.set_active_bucket("weekday", *tr.bucket_window("weekday"))
    assert tr.quiet_start == 2 and tr.quiet_end == 6 and tr.confidence == 0.8
    tr.weekend_quiet_start, tr.weekend_quiet_end, tr.weekend_confidence = 1, 7, 0.9
    tr.set_active_bucket("weekend", *tr.bucket_window("weekend"))
    assert tr.quiet_start == 1 and tr.quiet_end == 7 and tr.confidence == 0.9
    # 两桶独立,set_active_bucket 不覆盖桶自身字段
    assert tr.bucket_window("weekday") == (2, 6, 0.8)
    assert tr.bucket_window("weekend") == (1, 7, 0.9)
    print("  OK test_bucket_window_and_set_active_bucket")


# ── v8 双作息 state 集成测试 ──────────────────────────────


def test_on_user_message_buckets_by_day():
    """周六回复 → weekend 桶;周一回复 → weekday 桶(真实 holiday_parser,两天均为普通日)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        sat = datetime(2026, 8, 1, 10, 0, tzinfo=CST)
        assert sat.weekday() == 5
        s.on_user_message(sat)
        assert s.circadian.reply_days == [
            {"date": "2026-08-01", "hours": [10], "bucket": "weekend"},
        ]
        mon = datetime(2026, 7, 27, 10, 0, tzinfo=CST)
        assert mon.weekday() == 0
        s.on_user_message(mon)
        assert s.circadian.reply_days == [
            {"date": "2026-08-01", "hours": [10], "bucket": "weekend"},
            {"date": "2026-07-27", "hours": [10], "bucket": "weekday"},
        ]
    print("  OK test_on_user_message_buckets_by_day")


def test_on_user_message_cross_bucket_friday():
    """跨桶一天:周五 19:00 + 21:00 回复 → 两条独立条目(weekday + weekend),各自计入对应桶"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        fri = datetime(2026, 7, 31, 19, 0, tzinfo=CST)
        s.on_user_message(fri)
        s.on_user_message(fri.replace(hour=21))
        by_bucket = {d["bucket"]: d["hours"] for d in s.circadian.reply_days}
        assert by_bucket == {"weekday": [19], "weekend": [21]}, by_bucket
        assert len(s.circadian.reply_days) == 2
    print("  OK test_on_user_message_cross_bucket_friday")


def test_migrate_v7_state_backfills_buckets_and_weekday_window():
    """v7 格式:无 bucket 条目 + 旧单桶窗口 → 加载后补桶(weekday 日期 → weekday 桶;
    解析失败丢弃);weekday_* 继承旧窗口;weekend_* 保持默认;迁移幂等(再存再载不重复变化)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        v7 = {
            "_version": 7,
            "emotion": {},
            "cooldown": {},
            "circadian": {
                "reply_days": [
                    {"date": "2026-07-27", "hours": [10]},  # 周一 → weekday
                    {"date": "2026-08-01", "hours": [21]},  # 周六 → weekend
                    {"date": "garbage", "hours": [5]},      # 解析失败 → 丢弃
                ],
                "quiet_start": 2, "quiet_end": 7, "confidence": 0.8,
                "sample_days": 14,
            },
            "last_tick": "2026-08-01T10:00:00+08:00",
        }
        s.state_path.write_text(json.dumps(v7))
        s2 = _make_state(td)  # 重新加载 → 迁移
        by_bucket = {d["bucket"]: d["date"] for d in s2.circadian.reply_days}
        assert by_bucket == {"weekday": "2026-07-27", "weekend": "2026-08-01"}, by_bucket
        assert len(s2.circadian.reply_days) == 2  # garbage 被丢弃
        assert s2.circadian.weekday_quiet_start == 2
        assert s2.circadian.weekday_quiet_end == 7
        assert s2.circadian.weekday_confidence == 0.8
        assert s2.circadian.weekend_quiet_start == 0
        assert s2.circadian.weekend_quiet_end == 8
        assert s2.circadian.weekend_confidence == 0.0
        # 幂等:迁移后的状态再存再载 → 不重复变化
        s2.save(_backup=False, _increment_tick=False)
        s3 = _make_state(td)
        assert s3.circadian.reply_days == s2.circadian.reply_days
        assert s3.circadian.weekday_quiet_start == 2
        assert s3.circadian.weekday_quiet_end == 7
        assert s3.circadian.weekday_confidence == 0.8
        assert s3.circadian.weekend_confidence == 0.0
    print("  OK test_migrate_v7_state_backfills_buckets_and_weekday_window")


def test_migrate_does_not_inherit_weekend_snapshot_into_weekday():
    """v8 风格状态(weekend_* 已学习 + 兼容字段为周末快照)→ 重载时不得把 weekend 快照
    误继承为 weekday_*;weekday_* 保持 (0,8,0.0),weekend_* 保持 (2,7,0.8)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        v8 = {
            "_version": 8,
            "emotion": {},
            "cooldown": {},
            "circadian": {
                "reply_days": [
                    {"date": "2026-08-01", "hours": [21], "bucket": "weekend"},
                    {"date": "2026-07-27", "hours": [9], "bucket": "weekday"},
                ],
                "quiet_start": 2, "quiet_end": 7, "confidence": 0.8,  # 周六同步后的 weekend 快照
                "weekend_quiet_start": 2, "weekend_quiet_end": 7, "weekend_confidence": 0.8,
                "sample_days": 2,
            },
            "last_tick": "2026-08-01T10:00:00+08:00",
        }
        s.state_path.write_text(json.dumps(v8))
        s2 = _make_state(td)
        assert (s2.circadian.weekday_quiet_start, s2.circadian.weekday_quiet_end,
                s2.circadian.weekday_confidence) == (0, 8, 0.0), \
            "weekend 快照被误继承为 weekday_*"
        assert (s2.circadian.weekend_quiet_start, s2.circadian.weekend_quiet_end,
                s2.circadian.weekend_confidence) == (2, 7, 0.8)
    print("  OK test_migrate_does_not_inherit_weekend_snapshot_into_weekday")


def test_sync_quiet_window_selects_bucket_by_now():
    """按桶选窗:weekday(0,5,0.9) + weekend(2,7,0.8) → 周一 cooldown 0-5;周六 2-7;
    兼容字段(quiet_start/end/confidence)同步为当前生效桶快照"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s.circadian.weekday_quiet_start, s.circadian.weekday_quiet_end, s.circadian.weekday_confidence = 0, 5, 0.9
        s.circadian.weekend_quiet_start, s.circadian.weekend_quiet_end, s.circadian.weekend_confidence = 2, 7, 0.8
        mon = datetime(2026, 7, 27, 12, 0, tzinfo=CST)
        s._sync_quiet_window(mon)
        assert s.cooldown.quiet_window() == (0, 5)
        assert (s.circadian.quiet_start, s.circadian.quiet_end, s.circadian.confidence) == (0, 5, 0.9)
        sat = datetime(2026, 8, 1, 12, 0, tzinfo=CST)
        s._sync_quiet_window(sat)
        assert s.cooldown.quiet_window() == (2, 7)
        assert (s.circadian.quiet_start, s.circadian.quiet_end, s.circadian.confidence) == (2, 7, 0.8)
    print("  OK test_sync_quiet_window_selects_bucket_by_now")


def test_sync_quiet_window_falls_back_when_bucket_not_confident():
    """周末桶置信度 0.3 < min_confidence 0.5 → 周六回退配置默认 0-8;工作日桶(0.9)不受影响"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s.circadian.weekday_quiet_start, s.circadian.weekday_quiet_end, s.circadian.weekday_confidence = 0, 5, 0.9
        s.circadian.weekend_quiet_start, s.circadian.weekend_quiet_end, s.circadian.weekend_confidence = 2, 7, 0.3
        sat = datetime(2026, 8, 1, 12, 0, tzinfo=CST)
        s._sync_quiet_window(sat)
        assert s.cooldown.quiet_window() == (0, 8)  # 配置默认
        # 兼容字段仍同步为 weekend 桶快照(置信度不足只是不用它做门禁)
        assert (s.circadian.quiet_start, s.circadian.quiet_end, s.circadian.confidence) == (2, 7, 0.3)
        mon = datetime(2026, 7, 27, 12, 0, tzinfo=CST)
        s._sync_quiet_window(mon)
        assert s.cooldown.quiet_window() == (0, 5)
    print("  OK test_sync_quiet_window_falls_back_when_bucket_not_confident")


def test_migrate_v8_uses_holiday_and_makeup_buckets():
    """迁移补桶识别节假日/调休:国庆假期 2026-10-01(周四)→ weekend;
    调休上班日 2026-10-10(周六)→ weekday(真实 holiday_parser 内置 2026 数据覆盖)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        v7 = {
            "_version": 7,
            "emotion": {},
            "cooldown": {},
            "circadian": {
                "reply_days": [
                    {"date": "2026-10-01", "hours": [10]},  # 周四,国庆假期 → weekend
                    {"date": "2026-10-10", "hours": [21]},  # 周六,调休上班 → weekday
                    {"date": "2026-10-12", "hours": [9]},   # 周一,普通日 → weekday
                ],
            },
            "last_tick": "2026-10-10T10:00:00+08:00",
        }
        s.state_path.write_text(json.dumps(v7))
        s2 = _make_state(td)  # 重新加载 → 迁移
        assert [d["date"] for d in s2.circadian.reply_days] == \
            ["2026-10-01", "2026-10-10", "2026-10-12"]
        assert [d["bucket"] for d in s2.circadian.reply_days] == \
            ["weekend", "weekday", "weekday"]
    print("  OK test_migrate_v8_uses_holiday_and_makeup_buckets")


def test_sync_quiet_window_type_drift_defensive():
    """字符串类型漂移不崩:weekday start="abc"(不可强转)→ 回退默认窗口 (0,8);
    weekend start/end/conf 为字符串 → 强转后生效 (2,7)"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        s.circadian.weekday_quiet_start, s.circadian.weekday_quiet_end, s.circadian.weekday_confidence = "abc", 5, "0.9"
        s.circadian.weekend_quiet_start, s.circadian.weekend_quiet_end, s.circadian.weekend_confidence = 2, "7", "0.8"
        s._sync_quiet_window(datetime(2026, 7, 27, 12, 0, tzinfo=CST))  # 周一 → weekday 桶(start 非法 → 回退)
        assert s.cooldown.quiet_window() == (0, 8)  # 配置默认
        s._sync_quiet_window(datetime(2026, 8, 1, 12, 0, tzinfo=CST))  # 周六 → weekend 桶(字符串强转生效)
        assert s.cooldown.quiet_window() == (2, 7)
    print("  OK test_sync_quiet_window_type_drift_defensive")


def test_migrate_confidence_type_drift_gate():
    """迁移门控 confidence 类型漂移:字符串 "0.8" → 可解析,继承 weekday_* 为 0.8;
    "abc" → 强转失败视为 0,不继承(默认 0.0),均不崩"""
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        v7 = {"_version": 7, "emotion": {}, "cooldown": {},
              "circadian": {"quiet_start": 2, "quiet_end": 7, "confidence": "0.8", "sample_days": 14},
              "last_tick": "2026-08-01T10:00:00+08:00"}
        s.state_path.write_text(json.dumps(v7))
        s2 = _make_state(td)
        assert s2.circadian.weekday_quiet_start == 2
        assert s2.circadian.weekday_quiet_end == 7
        assert s2.circadian.weekday_confidence == 0.8
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td)
        v7 = {"_version": 7, "emotion": {}, "cooldown": {},
              "circadian": {"quiet_start": 2, "quiet_end": 7, "confidence": "abc", "sample_days": 14},
              "last_tick": "2026-08-01T10:00:00+08:00"}
        s.state_path.write_text(json.dumps(v7))
        s2 = _make_state(td)  # 不崩
        assert s2.circadian.weekday_confidence == 0.0  # 视为 0 → 不继承
        assert s2.circadian.weekend_confidence == 0.0
    print("  OK test_migrate_confidence_type_drift_gate")



