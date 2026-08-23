# schedule/facade.py — 日程域唯一门面（PR-3 AUD-005/006/007/008）。
import logging
import sys
from datetime import datetime, date as date_type
from pathlib import Path

from schedule.holiday import HolidayParser
from schedule.anniversary import AnniversaryManager
from schedule.override_store import OverrideStore
from schedule.plan_store import PlanStore


class ScheduleFacade:
    """聚合 holiday/anniversary/override/plan + 语义化只读查询。"""
    def __init__(self, base_dir: str, config: dict):
        self.base_dir = str(base_dir)
        self.config = config
        base = Path(self.base_dir)
        try:
            self.holiday = HolidayParser(str(base / "holidays.json"))
        except Exception as exc:
            print(f"[warn] HolidayParser 构造失败: {exc}", file=sys.stderr)
            self.holiday = None
        self.anniversaries = AnniversaryManager(self.base_dir)
        self.overrides = OverrideStore(self.base_dir)
        self.plans = PlanStore(self.base_dir)
        sched = config.get("schedule", {}) if isinstance(config, dict) else {}
        try:
            self.semester_start = date_type.fromisoformat(sched.get("semester_start", ""))
        except (ValueError, TypeError):
            self.semester_start = date_type(2026, 2, 23)
        self.semester_end = None
        se = sched.get("semester_end", "")
        if se:
            try:
                self.semester_end = date_type.fromisoformat(se)
            except (ValueError, TypeError):
                pass

    def is_holiday(self, now: datetime) -> bool:
        if self.holiday is None:
            return False
        try:
            return bool(self.holiday.is_holiday(now))
        except Exception as exc:
            logging.getLogger(__name__).debug("is_holiday fallback: %s", exc, exc_info=True)
            return False

    def is_makeup_workday(self, now: datetime) -> bool:
        if self.holiday is None:
            return False
        try:
            return bool(self.holiday.is_makeup_workday(now))
        except Exception as exc:
            logging.getLogger(__name__).debug("is_makeup_workday fallback: %s", exc, exc_info=True)
            return False

    def holiday_query(self, now: datetime):
        if self.holiday is None:
            return None
        try:
            return self.holiday.query(now)
        except Exception as exc:
            logging.getLogger(__name__).debug("holiday_query fallback: %s", exc, exc_info=True)
            return None

    def is_exam_season(self, now) -> bool:
        today = now.date() if isinstance(now, datetime) else now
        try:
            for it in self.overrides.intervals():
                d = it.get("date")
                if not isinstance(d, str):
                    continue
                try:
                    s = date_type.fromisoformat(d)
                    e = date_type.fromisoformat(it.get("end_date") or d)
                except (ValueError, TypeError):
                    continue
                if s <= today <= e:
                    return True
        except Exception:
            return False
        return False

    def calendar_policy(self):
        """供 circadian.bucket_for 注入，替代 state.holiday_parser 方法对象（AUD-007）。"""
        return (self.is_holiday, self.is_makeup_workday)

    def schedule_status(self, now):
        """只读快照：与 state/schedule 语义对齐，供 composer/context 直接消费。"""
        try:
            from schedule.sources import load_sources
            from schedule.day_plan import resolve_classes, current_period, _PERIOD_START
            from schedule.query import PERIOD_TIMES
            src = load_sources(self.base_dir, self.config)
            today = now.date() if isinstance(now, datetime) else now
            hq = src.holiday.query(now) if getattr(src, "holiday", None) else {"is_holiday": False, "holiday_name": None, "hint": "", "is_weekend": False, "is_makeup_workday": False}
            if src.break_state and (src.break_state.get("manual_override") or src.break_state.get("on_break")):
                return {"in_class": False, "current_course": None, "class_load": "free", "remaining_classes": 0, "total_classes": 0, "on_break": True, "break_reason": "手动无限期开启", "breaks": []}
            if hq.get("is_holiday"):
                return {"in_class": False, "current_course": None, "class_load": "free", "remaining_classes": 0, "total_classes": 0, "holiday": hq.get("holiday_name"), "holiday_hint": hq.get("hint"), "on_break": False, "breaks": []}
            if hq.get("is_weekend") and not hq.get("is_makeup_workday"):
                return {"in_class": False, "current_course": None, "class_load": "free", "remaining_classes": 0, "total_classes": 0, "weekend": True, "on_break": False, "breaks": []}
            if not src.schedule_valid:
                return None
            rc = resolve_classes(today, src)
            active = {p: c for p, c in rc.items() if not c.get("cancelled")}
            cp = current_period(now)
            result = {"in_class": cp in active, "on_break": False, "breaks": []}
            cur = active.get(cp)
            if cur:
                end_h, end_m = map(int, PERIOD_TIMES[cp][1].split(":"))
                end_time = now.replace(hour=end_h, minute=end_m, second=0)
                result["current_course"] = {**cur, "period": cp, "time": PERIOD_TIMES[cp], "minutes_remaining": max(0, (end_time - now).total_seconds() / 60)}
            else:
                result["current_course"] = None
            future = [p for p in sorted(active) if p > (cp or 0) and _PERIOD_START[p] > now.time()]
            nxt = next((active[p] for p in future), None)
            result["next_course"] = ({**nxt, "period": future[0], "time": PERIOD_TIMES[future[0]]} if nxt is not None else None)
            total = len(active)
            result["class_load"] = "free" if total == 0 else ("light" if total <= 2 else ("normal" if total <= 5 else "heavy"))
            result["remaining_classes"] = len(future)
            result["total_classes"] = total
            result["periods_today"] = [dict(c) for c in rc.values()]
            if hq.get("is_makeup_workday"):
                result["makeup_day"] = True
                result["makeup_reason"] = hq.get("hint")
            return result
        except Exception:
            return None

    def reload(self, new_config: dict):
        self.config = new_config
        sched = new_config.get("schedule", {}) if isinstance(new_config, dict) else {}
        try:
            self.semester_start = date_type.fromisoformat(sched.get("semester_start", ""))
        except (ValueError, TypeError):
            pass
        se = sched.get("semester_end", "")
        self.semester_end = None
        if se:
            try:
                self.semester_end = date_type.fromisoformat(se)
            except (ValueError, TypeError):
                pass
        base = Path(self.base_dir)
        try:
            self.holiday = HolidayParser(str(base / "holidays.json"))
        except Exception as exc:
            print(f"[warn] HolidayParser 热重载失败: {exc}", file=sys.stderr)
            self.holiday = None


class PlayProofProvider:
    """播放反证最小协议封装，决策层经门面解耦（AUD-006）。"""
    def __init__(self, config: dict, base_dir: str):
        from netease.service import NeteaseService
        self._svc = NeteaseService(config, str(base_dir))
    @property
    def enabled(self) -> bool:
        return bool(getattr(self._svc, "enabled", False))
    def fetch_play_proof(self, now: datetime):
        return self._svc.fetch_play_proof(now)
