"""state.schedule — 课表/假期/日程查询域（AUD-001/004/010）。"""

import sys
from datetime import datetime
from datetime import date as date_type


class ScheduleMixin:
    def _cache_fingerprint(self) -> str:
        parts = []
        for name in ("schedule_cache.json", "schedule_overrides.json", "break_state.json",
                     "holidays.json", "schedule_plan.json"):
            p = self._anchored(name)
            try:
                m = p.stat().st_mtime_ns if p.exists() else 0
            except OSError:
                m = 0
            parts.append(f"{name}:{m}")
        return "|".join(parts)

    def _resolved_for(self, now):
        from schedule.sources import load_sources
        from schedule.day_plan import resolve_classes
        key = f"{now.date().isoformat()}|{self._cache_fingerprint()}"
        if self._rc_cache.get("key") != key:
            src = load_sources(str(self._anchored(".")), self.config)
            self._rc_cache = {"key": key, "sources": src,
                              "classes": resolve_classes(now.date(), src)}
        return self._rc_cache["sources"], self._rc_cache["classes"]

    def availability(self, now: datetime, user_state: dict = None) -> float:
        from schedule.day_plan import availability_base, class_load_adjust, bayesian_adjust
        src, rc = self._resolved_for(now)
        res = availability_base(now, src)
        base = res["base"]
        if res["tier"] == "idle_school":
            base = class_load_adjust(base, rc, now)
        return bayesian_adjust(base, user_state, self.emotion, self.config)

    def schedule_status(self, now: datetime) -> dict | None:
        from schedule.day_plan import resolve_classes, _on_break, current_period, _PERIOD_START
        from schedule.sources import load_sources
        from schedule.query import PERIOD_TIMES
        src, rc = self._resolved_for(now)
        today = now.date() if isinstance(now, datetime) else now
        def _breaks_info():
            data = src.break_state
            if not data:
                return []
            out = []
            for b in data.get("breaks", []):
                try:
                    start = date_type.fromisoformat(b["start"]); end = date_type.fromisoformat(b["end"])
                except (ValueError, KeyError, TypeError):
                    continue
                out.append({"start": b["start"], "end": b["end"], "note": b.get("note", ""),
                            "active": start <= today <= end})
            return out
        on_break = _on_break(src.break_state, src.semester_start, src.semester_end, today)
        if on_break:
            return {"in_class": False, "current_course": None, "class_load": "free",
                    "remaining_classes": 0, "total_classes": 0, "on_break": True,
                    "break_reason": "学期未开始" if (src.semester_start and today < src.semester_start) else
                                    ("学期已结束" if (src.semester_end and today > src.semester_end) else
                                    ("手动无限期开启" if (src.break_state and (src.break_state.get("manual_override")
                                     or src.break_state.get("on_break"))) else "日期区间")),
                    "breaks": _breaks_info()}
        hq = src.holiday.query(now)
        if hq["is_holiday"]:
            return {"in_class": False, "current_course": None, "class_load": "free",
                    "remaining_classes": 0, "total_classes": 0, "holiday": hq["holiday_name"],
                    "holiday_hint": hq["hint"], "on_break": False, "breaks": _breaks_info()}
        if hq["is_weekend"] and not hq["is_makeup_workday"]:
            return {"in_class": False, "current_course": None, "class_load": "free",
                    "remaining_classes": 0, "total_classes": 0, "weekend": True,
                    "on_break": False, "breaks": _breaks_info()}
        if not src.schedule_valid:
            return None
        active = {p: c for p, c in rc.items() if not c.get("cancelled")}
        cp = current_period(now)
        result = {"in_class": cp in active, "on_break": False, "breaks": _breaks_info()}
        cur = active.get(cp)
        if cur:
            end_h, end_m = map(int, PERIOD_TIMES[cp][1].split(":"))
            end_time = now.replace(hour=end_h, minute=end_m, second=0)
            result["current_course"] = {**cur, "period": cp, "time": PERIOD_TIMES[cp],
                                        "minutes_remaining": max(0, (end_time - now).total_seconds() / 60)}
        else:
            result["current_course"] = None
        future = [p for p in sorted(active) if p > (cp or 0) and _PERIOD_START[p] > now.time()]
        nxt = next((active[p] for p in future), None)
        if nxt is not None:
            result["next_course"] = {**nxt, "period": future[0], "time": PERIOD_TIMES[future[0]]}
        else:
            result["next_course"] = None
        total = len(active)
        if total == 0:
            load = "free"
        elif total <= 2:
            load = "light"
        elif total <= 5:
            load = "normal"
        else:
            load = "heavy"
        result["class_load"] = load
        result["remaining_classes"] = len(future)
        result["total_classes"] = total
        result["periods_today"] = [dict(c) for c in rc.values()]
        if hq["is_makeup_workday"]:
            result["makeup_day"] = True
            result["makeup_reason"] = hq["hint"]
        return result

    def exam_season_now(self, now) -> bool:
        today = now.date() if isinstance(now, datetime) else now
        for it in self.override_store.intervals():
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
        return False

    def trigger_scale_now(self, now) -> dict:
        today = now.date() if isinstance(now, datetime) else now
        key = f"{today.isoformat()}|{self._cache_fingerprint()}"
        if getattr(self, "_scale_cache", None) and self._scale_cache.get("key") == key:
            return self._scale_cache["scale"]
        scale: dict = {}
        plan = self.plan_store.load()
        if plan:
            for mod in plan.get("modifiers", []):
                if not isinstance(mod, dict):
                    print(f"[schedule_plan] 非法 modifier(非 dict)已跳过: {mod!r}", file=sys.stderr)
                    continue
                ref = mod.get("ref", "")
                ts = mod.get("trigger_scale", {})
                if not isinstance(ts, dict):
                    continue
                if ref.startswith("fact:"):
                    item = self.override_store.by_id(ref[5:])
                    d = item.get("date") if item else None
                    if item is None or item.get("kind") != "exam_week" or not isinstance(d, str):
                        print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                        continue
                    try:
                        s = date_type.fromisoformat(d)
                        e = date_type.fromisoformat(item.get("end_date") or d)
                    except (ValueError, TypeError):
                        print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                        continue
                    if not (s <= today <= e):
                        continue
                elif ref.startswith("holiday:"):
                    r = (self.holiday_parser.range_of(ref[len("holiday:"):])
                         if self.holiday_parser else None)
                    if r is None:
                        print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                        continue
                    if not (r[0] <= today <= r[1]):
                        continue
                else:
                    print(f"[schedule_plan] dangling ref: {ref}", file=sys.stderr)
                    continue
                scale.update(ts)
        self._scale_cache = {"key": key, "scale": scale}
        return scale

    @property
    def break_state_path(self):
        return self._persistence.break_state_path

    def _read_break_state(self) -> dict | None:
        import json as _json
        bp = self.break_state_path
        if not bp.exists():
            return None
        try:
            return _json.loads(bp.read_text())
        except (ValueError, TypeError, OSError):
            return None

    def _in_break_range(self, today: date_type) -> bool:
        data = self._read_break_state()
        if not data:
            return False
        for b in data.get("breaks", []):
            try:
                start = date_type.fromisoformat(b["start"])
                end = date_type.fromisoformat(b["end"])
                if start <= today <= end:
                    return True
            except (ValueError, KeyError):
                continue
        return False

    @property
    def on_break(self) -> bool:
        from datetime import datetime as _dt
        from chiguo_time import CST as _CST
        data = self._read_break_state()
        if data:
            if data.get("manual_override") or data.get("on_break"):
                return True
        today = _dt.now(_CST).date()
        if self._in_break_range(today):
            return True
        if self.semester_start and today < self.semester_start:
            return True
        if self.semester_end and today > self.semester_end:
            return True
        return False
