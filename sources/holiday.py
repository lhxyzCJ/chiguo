# ============================================================
# sources/holiday.py — 节假日 / 纪念日观测源
# 只读包装 HolidayParser + AnniversaryManager（文件每次 observe 重读，取最新）。
# 产出：holiday.upcoming（7 天内有法定节假日开始）/
#       anniversary.upcoming（今天或 7 天内纪念日）。
# 最小实现：纪念日也可由 planner 从 commitments 读，这里只做 today + 7d 事实。
# ============================================================

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

from chiguo_time import CST
from schedule.anniversary import AnniversaryManager
from schedule.holiday import HolidayParser
from sources.base import Observation

_HORIZON_DAYS = 7


def _now_cst(now: datetime) -> datetime:
    return now.replace(tzinfo=CST) if now.tzinfo is None else now


def _anniversary_observation(name: str, occurrence, days_until: int,
                             now: datetime) -> Observation:
    return Observation(
        type="anniversary.upcoming",
        source="holiday",
        observed_at=now,
        # 过期 = 事件日「之后」的 00:00（事件当天全程有效）——若设为事件日 00:00，
        # days_until==0 的当天观测会被 active()/discover 直接判过期，当天机会永不可达。
        expires_at=datetime.combine(occurrence + timedelta(days=1), time.min, tzinfo=CST),
        payload={"name": name, "date": occurrence.isoformat(), "days_until": days_until},
    )


class HolidaySource:
    name = "holiday"

    def __init__(self, base_dir):
        self.base_dir = str(base_dir)

    def observe(self, now: datetime) -> list[Observation]:
        try:
            return self._observe(_now_cst(now))
        except Exception as exc:  # noqa: BLE001 — fail-safe：数据文件异常 → 空观测
            print(f"[sources.holiday] observe 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
            return []

    def _observe(self, now: datetime) -> list[Observation]:
        base = Path(self.base_dir)
        holiday = HolidayParser(str(base / "holidays.json"))
        anniversaries = AnniversaryManager(self.base_dir)
        today = now.date()
        observations: list[Observation] = []
        for key, (start, end) in holiday.all_ranges().items():
            days_until = (start - today).days
            if 0 <= days_until <= _HORIZON_DAYS:
                observations.append(Observation(
                    type="holiday.upcoming",
                    source=self.name,
                    observed_at=now,
                    # 同上：假期首日当天全程有效，过期 = 次日 00:00
                    expires_at=datetime.combine(start + timedelta(days=1), time.min, tzinfo=CST),
                    payload={"name": key.split("@", 1)[0], "start": start.isoformat(),
                             "end": end.isoformat(), "days_until": days_until},
                ))
        seen = set()
        for a in anniversaries.get_today(today):
            seen.add(a.name)
            observations.append(
                _anniversary_observation(a.name, today, 0, now))
        for a, days_until in anniversaries.get_upcoming(today, _HORIZON_DAYS):
            if a.name in seen:
                continue
            occurrence = today + timedelta(days=days_until)
            observations.append(
                _anniversary_observation(a.name, occurrence, days_until, now))
        observations.sort(key=lambda o: (o.payload["days_until"], o.type, o.payload["name"]))
        return observations
