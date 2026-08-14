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


def _holidays_cover_next_year(base_dir, next_year):
    """L-2 (#234): holidays.json 是否已含 next_year 数据（11-12 月 freshness 提示用）。"""
    try:
        hp = Path(base_dir) / "holidays.json"
        if not hp.exists():
            return False
        data = json.loads(hp.read_text())
        for r in data.get("holidays", {}).values():
            if str(r.get("start", "")).startswith(str(next_year)):
                return True
    except Exception:  # noqa: BLE001 - 读取失败按未覆盖处理
        return False
    return False


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
    try:
        holiday = HolidayParser(str(base / "holidays.json"))
    except Exception:
        # H-2 兜底:holidays.json override 意外异常 → 降级为仅内嵌节假日,不抛(replan 不被 traceback 中断)
        print(f"[schedule.sources] holidays.json 解析异常,降级仅用内嵌节假日", file=sys.stderr)
        holiday = HolidayParser()
    # L-2 (#234): 11-12 月且 holidays.json 未覆盖下一年 → stderr 提示（对齐 daemon._check_data_freshness）
    try:
        _now = date.today()
        if _now.month >= 11 and not _holidays_cover_next_year(base, _now.year + 1):
            print(f"[schedule.sources] {_now.year} 年节假日数据即将过期：请运行 "
                  f"python3 update_holidays.py {_now.year + 1} 生成 {_now.year + 1} 年模板（国务院通知发布后填实际日期）",
                  file=sys.stderr)
    except Exception:  # noqa: BLE001 - 提示不影响主流程
        pass
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
    sched_enabled = sched.get("enabled", True)
    if not sched_enabled:
        pass                      # 课表未启用 → unavailable 语义(即便存在陈旧缓存)
    elif schedule_cache_dict is not None:
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
