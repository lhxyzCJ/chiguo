# ============================================================
# schedule/sources.py — 读路径唯一入口(决策 1:Sources 聚合)
# 所有检索纯函数收 sources 参数零 I/O;load_sources 是唯一读文件入口。
# ============================================================

import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from schedule.holiday import HolidayParser
from schedule.anniversary import AnniversaryManager
from schedule.override_store import OverrideStore


@dataclass
class Sources:
    base_dir: str
    semester_start: date
    semester_end: date | None
    holiday: HolidayParser
    anniversaries: AnniversaryManager
    overrides: OverrideStore
    break_state: dict | None
    schedule: dict            # 归一化 {weekday(int): {period(int): course}}
    schedule_valid: bool      # 课表数据可用(否则 unavailable tier 1.0)


def normalize_schedule_cache(cache: dict) -> dict:
    """cache(list weeks + str 键)→ 内存(set weeks + int 键):唯一转换点(§4.1)。
    move/add 快照直接存规范形状,与本函数输出同形。"""
    schedule = {}
    for day_str, periods in (cache.get("schedule") or {}).items():
        day = int(day_str)
        schedule[day] = {}
        for period_str, course in periods.items():
            c = dict(course)
            c["weeks"] = set(c.get("weeks") or [])
            c["alternates"] = [{**a, "weeks": set(a.get("weeks") or [])}
                               for a in (c.get("alternates") or [])]
            schedule[day][int(period_str)] = c
    return schedule


def load_sources(base_dir: str, config: dict, schedule_cache_dict: dict | None = None) -> Sources:
    sched = config.get("schedule", {})
    ss_str = sched.get("semester_start", "")
    try:
        semester_start = date.fromisoformat(ss_str)
    except (ValueError, TypeError):
        semester_start = date(2026, 2, 23)
        print(f"[schedule.sources] [schedule].semester_start 缺失/非法({ss_str!r}),回退 2026-02-23",
              file=sys.stderr)
    semester_end = None
    se_str = sched.get("semester_end", "")
    if se_str:
        try:
            semester_end = date.fromisoformat(se_str)
        except (ValueError, TypeError):
            pass
    base = Path(base_dir)
    holiday = HolidayParser(str(base / "holidays.json"))
    anniversaries = AnniversaryManager(str(base))
    overrides = OverrideStore(str(base))
    break_state = None
    bp = base / "break_state.json"
    if bp.exists():
        try:
            break_state = json.loads(bp.read_text())
        except (json.JSONDecodeError, OSError, TypeError):
            break_state = None  # 损坏 → 静默 None(行为保持)
    schedule, valid = {}, False
    if schedule_cache_dict is not None:
        if isinstance(schedule_cache_dict, dict) and schedule_cache_dict.get("schedule"):
            schedule = normalize_schedule_cache(schedule_cache_dict)
            valid = bool(schedule)
    else:
        cp = base / "schedule_cache.json"
        if cp.exists():
            try:
                cache = json.loads(cp.read_text())
                if isinstance(cache, dict):
                    schedule = normalize_schedule_cache(cache)
                    valid = bool(schedule)
            except (json.JSONDecodeError, OSError, TypeError, ValueError, AttributeError):
                schedule, valid = {}, False  # 损坏 → 空(unavailable tier 语义,N10)
    return Sources(base_dir=str(base), semester_start=semester_start, semester_end=semester_end,
                   holiday=holiday, anniversaries=anniversaries, overrides=overrides,
                   break_state=break_state, schedule=schedule, schedule_valid=valid)
