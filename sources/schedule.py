# ============================================================
# sources/schedule.py — 课表观测源
# 包装既有 schedule 数据面（sources.load_sources + day_plan 纯函数），只读。
# 产出：schedule.state（当前快照，30 分钟有效）/ schedule.course_starting（<=30 分钟开课）。
# fail-safe：数据面任何异常 → 空列表 + stderr 告警，绝不崩 runtime。
# ============================================================

import sys
from datetime import datetime, time, timedelta

from chiguo_time import CST
from schedule.day_plan import availability_base, resolve_classes, week_number
from schedule.query import PERIOD_TIMES, current_period
from schedule.sources import load_sources
from sources.base import Observation

_PERIOD_START = {p: time.fromisoformat(start) for p, (start, _) in PERIOD_TIMES.items()}
_STATE_TTL = timedelta(minutes=30)
_STARTING_WINDOW = timedelta(minutes=30)


def _now_cst(now: datetime) -> datetime:
    return now.replace(tzinfo=CST) if now.tzinfo is None else now


def _class_load(n: int) -> str:
    if n == 0:
        return "free"
    if n <= 2:
        return "light"
    if n <= 5:
        return "normal"
    return "heavy"


class ScheduleSource:
    """每次 observe 重新 load_sources，取最新数据面（不缓存陈旧 store）。"""

    name = "schedule"

    def __init__(self, base_dir, config: dict | None = None):
        self.base_dir = str(base_dir)
        self.config = config or {}

    def observe(self, now: datetime) -> list[Observation]:
        try:
            return self._observe(_now_cst(now))
        except Exception as exc:  # noqa: BLE001 — fail-safe：数据面异常 → 空观测
            print(f"[sources.schedule] observe 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return []

    def _observe(self, now: datetime) -> list[Observation]:
        src = load_sources(self.base_dir, self.config)
        today = now.date()
        hq = src.holiday.query(today)
        on_break = availability_base(now, src)["tier"] == "break"
        day_off = (on_break or bool(hq.get("is_holiday"))
                   or (bool(hq.get("is_weekend")) and not hq.get("is_makeup_workday")))
        # 放假/节假日/周末不呈现缓存中的课（对齐 facade.schedule_status 语义）
        active = {} if day_off else {
            p: c for p, c in resolve_classes(today, src).items()
            if p is not None and not c.get("cancelled")}
        cp = current_period(now)
        in_class = cp in active
        observations = [Observation(
            type="schedule.state",
            source=self.name,
            observed_at=now,
            expires_at=now + _STATE_TTL,
            payload={
                "in_class": in_class,
                "class_load": _class_load(len(active)),
                "on_break": on_break,
                "holiday": hq.get("holiday_name"),
                "weekend": bool(hq.get("is_weekend")),
                "makeup_day": bool(hq.get("is_makeup_workday")),
                "current_course": active[cp]["course"] if in_class else None,
                "remaining_classes": len([p for p in active
                                          if p in _PERIOD_START and _PERIOD_START[p] > now.time()]),
                "week_num": week_number(today, src.semester_start),
            },
        )]
        observations.extend(self._course_starting(now, today, active))
        return observations

    def _course_starting(self, now: datetime, today, active: dict) -> list[Observation]:
        """距下一节课开始 <=30 分钟的事实；expires_at = 上课时刻。"""
        future = sorted(((p, _PERIOD_START[p]) for p in active
                         if p in _PERIOD_START and _PERIOD_START[p] > now.time()),
                        key=lambda x: x[1])
        if not future:
            return []
        period, start_t = future[0]
        start_dt = datetime.combine(today, start_t, tzinfo=CST)
        delta = start_dt - now
        if not (timedelta(0) < delta <= _STARTING_WINDOW):
            return []
        course = active[period]
        return [Observation(
            type="schedule.course_starting",
            source=self.name,
            observed_at=now,
            expires_at=start_dt,
            payload={"course": course.get("course", ""), "period": period,
                     "start": start_dt.isoformat(),
                     "minutes_until": int(delta.total_seconds() // 60),
                     "location": course.get("location", "")},
        )]
