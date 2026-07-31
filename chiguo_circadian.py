# ============================================================
# chiguo_circadian.py — 生物钟学习 v8
# 从主人回复时间学习睡眠/活跃时段,动态调整静默窗口
# v8:双作息(工作日/周末两桶独立学习,调休/假期修正)+ 听歌活跃反证
# 纯函数为主:可独立测试,不依赖状态文件
# ============================================================

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

MIN_HOURS = 24

BUCKETS = ("weekday", "weekend")


def bucket_for(dt: datetime, is_holiday: Callable, is_makeup_workday: Callable,
               weekend_start_hour: int = 20, weekend_end_hour: int = 20) -> str:
    """分桶:调休上班日 → "weekday";节假日(非调休) → "weekend";
    周五 weekend_start_hour 后、周六全天、周日 weekend_end_hour 前 → "weekend";其余 → "weekday"。
    判定顺序:调休优先(is_makeup_workday 为真直接 weekday),再节假日(is_holiday 为真直接 weekend),
    然后时间规则(dt.weekday(): 4 且 hour >= start → weekend;5 → weekend;6 且 hour < end → weekend)。"""
    if is_makeup_workday(dt):
        return "weekday"
    if is_holiday(dt):
        return "weekend"
    w = dt.weekday()
    if w == 4 and dt.hour >= weekend_start_hour:
        return "weekend"
    if w == 5:
        return "weekend"
    if w == 6 and dt.hour < weekend_end_hour:
        return "weekend"
    return "weekday"


def estimate_sleep_window(hour_counts: list[int], sample_days: int,
                          history_days: int, min_sample_days: int = 7,
                          min_width: int = 5, max_width: int = 12) -> dict | None:
    """
    从 24 小时回复频率中估计睡眠窗口(允许跨午夜)。

    算法:对 24 小时做环形滑动窗口(width ∈ [min_width, max_width]),
    取回复总数最小的窗口;(sum, width, start) 元组最小者胜(字典序:
    总数最小 → 宽度最小 → 起点最早),保证确定性。

    置信度 = 数据完整度(sample_days/history_days) × 窗口安静度
    (1 - 窗口均回复/全天均回复)。

    返回 {"quiet_start", "quiet_end", "width", "confidence", "sample_days"}
    quiet_end 不含 end(与 cooldown 窗口语义一致,qe < qs 表示跨午夜)。
    数据不足/无数据 → None(调用方回退配置默认窗口)。
    """
    if len(hour_counts) != MIN_HOURS:
        return None
    if min_width < 1 or max_width < min_width:
        return None
    if sample_days < min_sample_days:
        return None
    total = sum(hour_counts)
    if total <= 0:
        return None

    best: tuple | None = None  # (sum, width, start)
    for width in range(min_width, max_width + 1):
        for start in range(MIN_HOURS):
            s = sum(hour_counts[(start + i) % MIN_HOURS] for i in range(width))
            key = (s, width, start)
            if best is None or key < best:
                best = key

    sum_w, width, start = best
    end = (start + width) % MIN_HOURS
    avg_hour = total / MIN_HOURS
    window_mean = sum_w / width
    quietness = 1.0 - min(1.0, window_mean / max(avg_hour, 1e-9))
    completeness = min(1.0, sample_days / max(1, history_days))
    confidence = round(completeness * quietness, 3)
    return {
        "quiet_start": start,
        "quiet_end": end,
        "width": width,
        "confidence": confidence,
        "sample_days": sample_days,
    }


def track_reply_hour(reply_days: list[dict], now: datetime,
                     history_days: int, bucket: str) -> list[dict]:
    """记录一次回复的小时(带分桶)。返回滚动更新后的 reply_days(保留最近 history_days 天)。

    v8:新条目带 "bucket" 字段;同日同桶合并,同日不同桶 → 分开条目(一天可跨桶,如周五 20:00 前后)。
    无 bucket 字段的旧条目不参与合并(迁移由 state 层保证加载后都有 bucket)。
    损坏记录(非法日期/缺 hours 键)不崩溃:非法日期回退按 now 修剪。
    """
    today = now.strftime("%Y-%m-%d")
    for d in reply_days:
        if not isinstance(d, dict):
            continue
        if d.get("date") == today and d.get("bucket") == bucket:
            if not isinstance(d.get("hours"), list):
                d["hours"] = []
            d["hours"].append(now.hour)
            break
    else:
        reply_days.append({"date": today, "hours": [now.hour], "bucket": bucket})
    latest = max((d.get("date", "") for d in reply_days
                  if isinstance(d, dict) and isinstance(d.get("date"), str)),
                 default="")
    try:
        cutoff = (datetime.strptime(latest, "%Y-%m-%d")
                  - timedelta(days=history_days)).strftime("%Y-%m-%d")
    except ValueError:
        cutoff = (now - timedelta(days=history_days)).strftime("%Y-%m-%d")
    return [d for d in reply_days
            if isinstance(d, dict) and isinstance(d.get("date"), str)
            and d.get("date", "") > cutoff]


def aggregate_hours(reply_days: list[dict], bucket: str | None = None) -> list[int]:
    """汇总 reply_days → 24 小时计数数组(长度恒 24)。

    bucket 不为 None 时只统计该桶条目;无 bucket 字段的条目视为不属于任何桶,过滤时不计。
    """
    counts = [0] * MIN_HOURS
    for d in reply_days:
        if not isinstance(d, dict):
            continue
        if bucket is not None and d.get("bucket") != bucket:
            continue
        hours = d.get("hours", [])
        if not isinstance(hours, list):
            continue
        for h in hours:
            try:
                counts[int(h)] += 1
            except (ValueError, TypeError, IndexError):
                continue
    return counts


def count_sample_days(reply_days: list[dict], bucket: str | None = None) -> int:
    """有回复记录的天数(按桶过滤,同 aggregate_hours)。"""
    return sum(1 for d in reply_days
               if isinstance(d, dict) and d.get("hours")
               and (bucket is None or d.get("bucket") == bucket))


def _bucket_dates(days: list[dict], bucket: str) -> set[str]:
    """某桶内有数据(hours 非空)的日期集合(供 sample_days 跨数组按日期去重)。"""
    return {d.get("date") for d in days
            if isinstance(d, dict) and d.get("hours")
            and d.get("bucket") == bucket
            and isinstance(d.get("date"), str)}


@dataclass
class CircadianTracker:
    """作息学习器。存 chiguo_state.json 的 "circadian" 字段。

    v8:双作息——reply_days/active_days 条目带 "bucket",两桶独立估计写 weekday_*/weekend_*;
    quiet_start/quiet_end/confidence 保留为"当前生效桶快照"(由 set_active_bucket/_sync_quiet_window 更新);
    sample_days 由 recompute 维护(跨桶按日期去重的有数据天数)。
    """
    reply_days: list[dict] = field(default_factory=list)  # [{"date": "YYYY-MM-DD", "hours": [0-23,...], "bucket": "weekday"|"weekend"}]
    active_days: list[dict] = field(default_factory=list)  # 听歌活跃,同结构
    # 兼容保留(当前生效桶快照,由 set_active_bucket/_sync_quiet_window 更新):
    quiet_start: int = 0    # 学习到的睡眠窗口起点(未达标时保持默认)
    quiet_end: int = 8      # 学习到的睡眠窗口终点(不含)
    confidence: float = 0.0  # 学习置信度(0-1)
    sample_days: int = 0     # 有数据的天数(跨桶按日期去重),由 recompute 维护
    # 双桶(独立学习):
    weekday_quiet_start: int = 0
    weekday_quiet_end: int = 8
    weekday_confidence: float = 0.0
    weekend_quiet_start: int = 0
    weekend_quiet_end: int = 8
    weekend_confidence: float = 0.0

    def record(self, now: datetime, history_days: int = 14, bucket: str = "weekday"):
        """记录一次回复时间(带桶)。reply_days 非列表(手改损坏) → 重置为空列表,不崩溃。"""
        if not isinstance(self.reply_days, list):
            self.reply_days = []
        self.reply_days = track_reply_hour(self.reply_days, now, history_days, bucket)

    def record_active(self, now: datetime, history_days: int = 14, bucket: str = "weekday"):
        """记录一次听歌活跃时间(带桶)。active_days 非列表 → 重置为空列表,不崩溃。"""
        if not isinstance(self.active_days, list):
            self.active_days = []
        self.active_days = track_reply_hour(self.active_days, now, history_days, bucket)

    def recompute(self, min_sample_days: int = 7, history_days: int = 14,
                  min_width: int = 5, max_width: int = 12) -> dict | None:
        """重算学习窗口:两桶独立估计(reply+active 逐小时合并计数)。

        桶内数据不足 → 该桶保持当前值(不覆盖);sample_days 更新为有数据天数(跨桶按日期去重)。
        返回第一桶(weekday 优先)成功估计的窗口,两桶均无估计 → None。
        """
        result = None
        dates_all: set[str] = set()
        for bucket in BUCKETS:
            reply_dates = _bucket_dates(self.reply_days, bucket)
            active_dates = _bucket_dates(self.active_days, bucket)
            dates_all |= reply_dates | active_dates
            counts = [a + b for a, b in zip(
                aggregate_hours(self.reply_days, bucket),
                aggregate_hours(self.active_days, bucket))]
            est = estimate_sleep_window(
                counts, len(reply_dates | active_dates),
                history_days, min_sample_days, min_width, max_width,
            )
            if est is None:
                continue
            setattr(self, f"{bucket}_quiet_start", est["quiet_start"])
            setattr(self, f"{bucket}_quiet_end", est["quiet_end"])
            setattr(self, f"{bucket}_confidence", est["confidence"])
            if result is None:
                result = est
        self.sample_days = len(dates_all)
        return result

    def bucket_window(self, bucket: str) -> tuple[int, int, float]:
        """返回该桶 (start, end, confidence)。未知桶 → 返回默认 (0, 8, 0.0)。"""
        if bucket not in BUCKETS:
            return (0, 8, 0.0)
        return (getattr(self, f"{bucket}_quiet_start"),
                getattr(self, f"{bucket}_quiet_end"),
                getattr(self, f"{bucket}_confidence"))

    def set_active_bucket(self, bucket: str, start: int, end: int,
                          confidence: float):
        """把某桶窗口同步到兼容字段 quiet_start/quiet_end/confidence(供 _sync_quiet_window 与门禁使用)。
        未知桶 → 不写入,返回。"""
        if bucket not in BUCKETS:
            return
        self.quiet_start = int(start)
        self.quiet_end = int(end)
        self.confidence = float(confidence)
