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


