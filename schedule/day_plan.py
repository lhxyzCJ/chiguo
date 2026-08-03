# ============================================================
# schedule/day_plan.py — 检索层:纯事实多日窗口 + 窄确定性原语(§4/§5.1)
# 全部纯函数收 sources 参数,零 I/O。消费方:chiguo_state(3a)/--attention(5)/replan(7)。
# ============================================================

from datetime import date, time, timedelta

from schedule.query import current_period, PERIOD_TIMES

_PERIOD_START = {p: time.fromisoformat(start) for p, (start, _) in PERIOD_TIMES.items()}


def week_number(d: date, semester_start: date) -> int:
    """周一归一化的学期周次(§4.1):周界对齐周一;semester_start 恰为周一时与原式逐值一致;
    学期结束后不钳制(无课匹配自然为空)。"""
    offset = (d - semester_start).days + semester_start.weekday()
    return max(1, offset // 7 + 1)


def week_courses(schedule: dict, semester_start: date, week_num: int) -> dict:
    """{weekday: {period: course}} active 过滤:weeks 含该周 + alternates 周次互斥。
    与 schedule/query.py:53-63 共用同一逻辑(三处委托:day_plan/T3/schedule_query)。"""
    out = {}
    for weekday, periods in schedule.items():
        act = {}
        for period, entry in periods.items():
            for course in [entry] + entry.get("alternates", []):
                weeks = course.get("weeks", set())
                if weeks and week_num in weeks:
                    act[period] = course
                    break
        out[weekday] = act  # 空日也保留键(测试契约:[0] 索引空日;调用方一律 .get(day, {}))
    return out


def _base_entries(weekday: int, schedule: dict, semester_start: date, d: date) -> dict:
    w = week_number(d, semester_start)
    base = week_courses(schedule, semester_start, w).get(weekday, {})
    entries = {}
    for period, course in base.items():
        entries[period] = {"period": period, "course": course.get("course", ""),
                           "teacher": course.get("teacher", ""),
                           "location": course.get("location", ""),
                           "source": "schedule", "cancelled": False}
    return entries


def resolve_classes(d: date, sources) -> dict:
    """该日呈现的课程 {period: 平铺条目}。
    move 双槽位语义(§3.2 M10/HIGH-1):按 created_at 后写覆盖;① 目标槽替换呈现(基底被遮蔽)
    ② 后写 cancel 命中目标槽 → 移除 move 课恢复基底 ③ 源槽命中 → no-op ④ 整条删除只经 remove 链路。
    cancel:基底 → cancelled:true 保留;add/move 呈现 → 移除(覆盖先写例外)。"""
    entries = _base_entries(d.weekday(), sources.schedule, sources.semester_start, d)
    base_by_period = {p: dict(e) for p, e in entries.items()}
    for ov in sorted(sources.overrides.for_date(d), key=lambda x: x.get("created_at", "")):
        kind = ov["kind"]
        p = ov.get("period")
        if kind == "cancel":
            if p in entries:
                if entries[p]["source"] == "override":
                    if entries[p].get("action") == "move" and p in base_by_period:
                        entries[p] = dict(base_by_period[p])   # ② 恢复基底(不取消)
                    else:
                        del entries[p]                          # 后写 cancel 覆盖先写 add
                else:
                    entries[p]["cancelled"] = True
        elif kind == "add":
            entries[p] = {"period": p, "course": ov.get("course", {}).get("course", ""),
                          "teacher": ov.get("course", {}).get("teacher", ""),
                          "location": ov.get("course", {}).get("location", ""),
                          "source": "override", "action": "add", "cancelled": False}
        elif kind == "move":
            to_d = ov.get("to_date")
            if ov["date"] == d.isoformat() and p in entries:
                del entries[p]                                  # 源日:源槽清空(③ 无对象 → no-op)
            if to_d is None or to_d == d.isoformat():
                entries[ov["to_period"]] = {"period": ov["to_period"],
                                            "course": ov.get("course", {}).get("course", ""),
                                            "teacher": ov.get("course", {}).get("teacher", ""),
                                            "location": ov.get("course", {}).get("location", ""),
                                            "source": "override", "action": "move", "cancelled": False}
    return entries


def _on_break(break_state: dict | None, semester_end: date | None, today: date) -> bool:
    if break_state:
        if break_state.get("manual_override") or break_state.get("on_break"):
            return True
        for b in break_state.get("breaks", []):
            try:
                s, e = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if s <= today <= e:
                return True
    if semester_end and today > semester_end:
        return True
    return False


def _break_projection(break_state: dict | None, d: date) -> dict | None:
    if not break_state:
        return None
    if break_state.get("manual_override") or break_state.get("on_break"):
        return {"manual": True, "start": None, "end": None, "note": break_state.get("note", "")}
    for b in break_state.get("breaks", []):
        try:
            s, e = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
        except (KeyError, ValueError, TypeError):
            continue
        if s <= d <= e:
            return {"manual": False, "start": b["start"], "end": b["end"],
                    "note": b.get("note", "")}
    return None


def _in_exam(today: date, intervals: list[dict]) -> bool:
    for it in intervals:
        s = date.fromisoformat(it["date"])
        e = date.fromisoformat(it.get("end_date") or it["date"])
        if s <= today <= e:
            return True
    return False


def day_plan(start: date, days: int, sources) -> dict:
    """多日纯事实窗口(§4):无优先级字段;break 恒在(null 或投影);classes 平铺(M8)。"""
    end = start + timedelta(days=days - 1)
    dates = []
    hp = sources.holiday
    for i in range(days):
        d = start + timedelta(days=i)
        hq = hp.query(d)
        entry = {"date": d.isoformat(), "break": _break_projection(sources.break_state, d),
                 "weekend": hq["is_weekend"], "makeup": hq["is_makeup_workday"],
                 "anniversary": [], "reminders": [], "classes": [], "facts": []}
        if hq["is_holiday"]:
            entry["holiday"] = {"name": hq["holiday_name"]}
        mmdd = d.strftime("%m-%d")
        for a in sources.anniversaries.visible_items():
            if a.get("type") == "anniversary" and a.get("date") == mmdd:
                entry["anniversary"].append({"name": a["name"], "days_until": i})
        for r in sources.overrides.reminders_in(start, end):
            if r["date"] == d.isoformat():
                entry["reminders"].append({"label": r["label"], "days_until": i})
        for it in sources.overrides.intervals():
            s = date.fromisoformat(it["date"])
            e = date.fromisoformat(it.get("end_date") or it["date"])
            if s <= d <= e:
                entry["facts"].append({"kind": it["kind"], "label": it.get("label", "")})
        entry["classes"] = [dict(c) for c in resolve_classes(d, sources).values()]
        dates.append(entry)
    return {"dates": dates}


def is_in_class(now, sources) -> bool:
    """规则链 on_break > 法定节假日 > 周末 > 调休补课日 > 课表(含例外);cancel 槽位不算课(§5.1)。"""
    today = now.date() if hasattr(now, "date") else now
    hp = sources.holiday
    if _on_break(sources.break_state, sources.semester_end, today):
        return False
    if hp.is_holiday(today):
        return False
    if not hp.is_school_day(today):
        return False
    if not sources.schedule_valid:
        return False
    cp = current_period(now)
    if cp is None:
        return False
    classes = resolve_classes(today, sources)
    return cp in classes and not classes[cp].get("cancelled")


def availability_base(now, sources) -> dict:
    """第一层学校日判定(§5.1):{"base", "tier"}。
    重叠优先级(C5,与现码三处统一):考周×寒暑假 → break 0.85;考周×法定节假日 → 节假日 0.85
    (exam 检查嵌套在 elif not is_holiday 内);考周×周末/调休 → exam 0.5(in_exam 先于周末判据)。"""
    today = now.date() if hasattr(now, "date") else now
    hp = sources.holiday
    if _on_break(sources.break_state, sources.semester_end, today):
        return {"base": 0.85, "tier": "break"}
    if not hp.is_holiday(today):
        if _in_exam(today, sources.overrides.intervals()):
            return {"base": 0.5, "tier": "exam"}
        if not hp.is_school_day(today):
            return {"base": 0.85, "tier": "non_school"}   # 周末(非调休)
        if not sources.schedule_valid:
            return {"base": 1.0, "tier": "unavailable"}
        return {"base": 0.85, "tier": "idle_school"}
    return {"base": 0.85, "tier": "non_school"}


def class_load_adjust(base: float, resolved_classes: dict, now) -> float:
    """第二层(仅 idle_school):上课中 heavy 0.05/normal 0.08/light 0.12(按当日有效节数档位);
    课间按剩余节数:先判 0 → 0.85,再判 1 → 0.70,其余 → 0.50(桶无重叠,实现顺序钉死)。
    R3:课间 remaining = 尚未开始的节数(该节起始时间 > now.time()),0/1/其余桶序钉死。
    cancelled 不计入有效节数/剩余 → 例外取消 → availability 上升。"""
    active = {p: c for p, c in resolved_classes.items() if not c.get("cancelled")}
    cp = current_period(now)
    if cp in active:
        n = len(active)
        if n <= 2:
            return 0.12
        if n <= 5:
            return 0.08
        return 0.05
    remaining = len([p for p in active if _PERIOD_START[p] > now.time()])
    if remaining == 0:
        return 0.85
    if remaining == 1:
        return 0.70
    return 0.50


def bayesian_adjust(base: float, user_state: dict, emotion, config: dict) -> float:
    """第三层(§5.1):现逻辑原样保留——高置信 sleeping → 0.0;busy ×0.5;
    needs_care → min(×1.2, 0.95);anxiety 超阈值 → ×0.3。"""
    try:
        if user_state is None:
            return base
        most_likely = user_state.get("most_likely", "browsing")
        confidence = user_state.get("confidence", 0.0)
        if most_likely == "sleeping" and confidence > config.get("bayesian", {}).get(
                "min_confidence_for_block", 0.5):
            return 0.0
        if most_likely == "busy":
            base *= 0.5
        elif most_likely == "needs_care":
            base = min(base * 1.2, 0.95)
        if emotion.anxiety > config.get("cooldown", {}).get("anxiety_block_threshold", 70.0):
            base *= 0.3
    except Exception:
        pass
    return base
