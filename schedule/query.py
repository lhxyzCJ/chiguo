# schedule/query.py — 上课状态计算（策略层,纯函数）
# 输入解析后的 schedule dict + 学期起始日期,输出上课状态 dict（无 I/O）。

from datetime import date, datetime

# ── 节次 → 时间映射（中国大学标准） ──────────────────────

PERIOD_TIMES = {
    1:  ("08:00", "08:45"),
    2:  ("08:50", "09:35"),
    3:  ("10:00", "10:45"),
    4:  ("10:50", "11:35"),
    5:  ("14:00", "14:45"),
    6:  ("14:50", "15:35"),
    7:  ("16:00", "16:45"),
    8:  ("16:50", "17:35"),
    9:  ("19:00", "19:45"),
    10: ("19:50", "20:35"),
    11: ("20:40", "21:25"),
}


def current_period(now: datetime) -> int | None:
    """根据当前时间返回所在节次"""
    current_minutes = now.hour * 60 + now.minute
    for period, (start_str, end_str) in PERIOD_TIMES.items():
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= current_minutes <= end:
            return period

        # 课间：如果在两节课之间，返回前一节
        next_start = None
        if period + 1 in PERIOD_TIMES:
            ns = PERIOD_TIMES[period + 1][0]
            nsh, nsm = map(int, ns.split(":"))
            next_start = nsh * 60 + nsm

        if end < current_minutes and (next_start is None or current_minutes < next_start):
            # 在课间休息中
            return None  # 课间视为不在上课

        if period == max(PERIOD_TIMES.keys()) and current_minutes > end:
            return None

    return None


def schedule_query(schedule: dict, semester_start: date, now: datetime) -> dict:
    """
    计算当前上课状态（纯函数,不触碰文件/缓存）。
    schedule: {weekday: {period: course_info}}（course_info 可含 alternates 键）
    返回:
      {
        "in_class": bool,           # 是否在上课
        "current_course": dict|null, # 当前课程信息
        "next_course": dict|null,   # 下一节课
        "periods_today": [...],     # 今天所有课
        "class_load": "heavy"|"normal"|"light"|"free",
      }
    available 键由数据面（ScheduleParser.query）补充。
    """
    weekday = now.weekday()  # 0=Mon ... 6=Sun
    current_period_no = current_period(now)
    # 学期第几周（从 semester_start 算起）
    week_num = max(1, (now.date() - semester_start).days // 7 + 1)

    today_courses = schedule.get(weekday, {})

    # 筛选本周有效的课程。
    # 合并单元格（alternates）同一时段多门课周次互斥：取 weeks 含本周的那门
    active_courses = {}
    for period, entry in today_courses.items():
        for course in [entry] + entry.get("alternates", []):
            weeks = course.get("weeks", set())
            if weeks and week_num in weeks:
                active_courses[period] = course
                break

    # 当前课程
    in_class = False
    current_course = None
    if current_period_no in active_courses:
        in_class = True
        current_course = {k: v for k, v in active_courses[current_period_no].items()
                          if k != "alternates"}
        current_course["period"] = current_period_no
        current_course["time"] = PERIOD_TIMES.get(current_period_no, ("?", "?"))
        # 距离下课剩余分钟数
        end_time_str = PERIOD_TIMES.get(current_period_no, ("?", "?"))[1]
        end_h, end_m = map(int, end_time_str.split(":"))
        end_time = now.replace(hour=end_h, minute=end_m, second=0)
        current_course["minutes_remaining"] = max(0, (end_time - now).total_seconds() / 60)

    # 下一节课
    next_course = None
    next_free_at = None
    sorted_periods = sorted(active_courses.keys())
    for p in sorted_periods:
        if p > (current_period_no or 0):
            next_course = {k: v for k, v in active_courses[p].items()
                           if k != "alternates"}
            next_course["period"] = p
            next_course["time"] = PERIOD_TIMES.get(p, ("?", "?"))
            break
    if next_course is None and in_class:
        # 下课后就自由了
        next_free_at = PERIOD_TIMES.get(current_period_no, ("?", "?"))[1]

    # 课业负担
    total_periods = len(active_courses)
    remaining = len([p for p in sorted_periods if p > (current_period_no or 0)])
    if total_periods == 0:
        class_load = "free"
    elif total_periods <= 2:
        class_load = "light"
    elif total_periods <= 5:
        class_load = "normal"
    else:
        class_load = "heavy"

    return {
        "in_class": in_class,
        "current_course": current_course,
        "next_course": next_course,
        "next_free_at": next_free_at,
        "periods_today": [
            {"period": p, **{k: v for k, v in active_courses[p].items()
                             if k != "alternates"}}
            for p in sorted(active_courses.keys())
        ],
        "class_load": class_load,
        "remaining_classes": remaining,
        "total_classes": total_periods,
    }
