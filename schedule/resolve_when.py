# ============================================================
# schedule/resolve_when.py — 相对时间换算(§4.2,日期零 LLM 的写入侧闭环)
# 纯换算三参 (when, today, semester_start) -> (start, end),不持有 kind。
# 值级/结构拒绝在此内执行,信号 = ResolveReject(ValueError 子类,二十轮钉);
# 按 kind 的形态约束/分端点过去校验/字段落点/学期边界属 api 层(持 kind 调用)。
# ============================================================

from datetime import date, timedelta

from schedule.anniversary import mmdd_to_date


class ResolveReject(ValueError):
    """换算拒绝。category ∈ {"ambiguous", "invalid_value"} → api 映射 H5 文案。"""

    def __init__(self, category: str, message: str):
        super().__init__(message)
        self.category = category


def _as_int(v, name: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise ResolveReject("invalid_value", f"{name} 非整数")
    return v


def _mmdd(v, today: date) -> date:
    """两位月两位日(前导零,格式钉死);今年已过 → 明年(当天留今年);02-29 经 mmdd_to_date 兜底。"""
    if not isinstance(v, str) or len(v) != 5 or v[2] != "-":
        raise ResolveReject("invalid_value", f"MM-DD 非法: {v!r}")
    try:
        mm, dd = int(v[:2]), int(v[3:])
    except ValueError:
        raise ResolveReject("invalid_value", f"MM-DD 非法: {v!r}")
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        raise ResolveReject("invalid_value", f"MM-DD 非法: {v!r}")
    try:
        d_this = mmdd_to_date(v, today.year)
        d_next = mmdd_to_date(v, today.year + 1)
    except ValueError:
        raise ResolveReject("invalid_value", f"MM-DD 非法: {v!r}")
    return d_this if d_this >= today else d_next


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _transcribe(part, anchor_year: int) -> date:
    """start-end 端点转写:显式 YYYY-MM-DD 直用;MM-DD 按 anchor_year(显式端裁定,已过不推断)。"""
    if isinstance(part, str) and len(part) == 10:
        try:
            return date.fromisoformat(part)
        except ValueError:
            raise ResolveReject("invalid_value", f"端点非法: {part!r}")
    if isinstance(part, str) and len(part) == 5 and part[2] == "-":
        try:
            mm, dd = int(part[:2]), int(part[3:])
        except ValueError:
            raise ResolveReject("invalid_value", f"MM-DD 非法: {part!r}")
        if not (1 <= mm <= 12 and 1 <= dd <= 31):
            raise ResolveReject("invalid_value", f"MM-DD 非法: {part!r}")
        try:
            return mmdd_to_date(part, anchor_year)
        except ValueError:
            raise ResolveReject("invalid_value", f"MM-DD 非法: {part!r}")
    raise ResolveReject("invalid_value", f"start/end 端点非法: {part!r}")


def _resolve_start_end(when: dict, today: date) -> tuple[date, date]:
    """C1 算法:① 转写(混合端按显式年裁定)② end<start 且 end 月份<start 月份 → end 加一年
    ③ 终校验:倒序拒 / 跨度>60 拒(恰 60 允许) / start==end 单日退化。"""
    s, e = when["start"], when["end"]
    s_explicit = isinstance(s, str) and len(s) == 10
    e_explicit = isinstance(e, str) and len(e) == 10
    anchor = today.year
    if s_explicit:
        try:
            anchor = date.fromisoformat(s).year
        except ValueError:
            raise ResolveReject("invalid_value", f"start 非法: {s!r}")
    elif e_explicit:
        try:
            anchor = date.fromisoformat(e).year
        except ValueError:
            raise ResolveReject("invalid_value", f"end 非法: {e!r}")
    start = _transcribe(s, anchor)
    end = _transcribe(e, anchor)
    if end < start and end.month < start.month and not (s_explicit and e_explicit):
        # 跨年推断仅限端点非显式年(MM-DD);显式双端点严格 end>=start,倒序直接拒绝
        end = date(end.year + 1, end.month, end.day)
    if end < start:
        raise ResolveReject("invalid_value", "start-end 倒序")
    if (end - start).days > 60:
        raise ResolveReject("invalid_value", "跨度 > 60 天")
    return (start, end)


def resolve_when(when, today: date, semester_start: date) -> tuple[date, date]:
    """七形态令牌纯换算 → (start, end)。拒绝抛 ResolveReject。
    R1:week_offset 相对 today 锚定(周一归一),semester_start 仅供 api 学期边界检查。"""
    if not isinstance(when, dict) or not when:
        raise ResolveReject("ambiguous", "空 when/None → 歧义拒绝")
    keys = set(when)
    if keys == {"date"}:
        v = when["date"]
        if isinstance(v, str) and len(v) == 10:
            try:
                d = date.fromisoformat(v)
            except ValueError:
                raise ResolveReject("invalid_value", f"date 非法: {v!r}")
            return (d, d)
        d = _mmdd(v, today)
        return (d, d)
    if keys == {"days"}:
        n = _as_int(when["days"], "days")
        if n < 1:
            raise ResolveReject("invalid_value", "days 须 ≥ 1")
        d = today + timedelta(days=n)
        return (d, d)
    if keys == {"weekday"}:
        w = _as_int(when["weekday"], "weekday")
        if not 1 <= w <= 7:
            raise ResolveReject("invalid_value", "weekday 越界(1-7,ISO 周一=1)")
        days_ahead = w - today.isoweekday()
        if days_ahead <= 0:
            days_ahead += 7
        d = today + timedelta(days=days_ahead)
        return (d, d)
    if keys == {"week_offset"}:
        k = _as_int(when["week_offset"], "week_offset")
        if k not in (0, 1):
            raise ResolveReject("invalid_value", "week_offset 仅 0|1")
        monday = _monday_of(today) + timedelta(weeks=k)
        return (monday, monday + timedelta(days=4))  # 区间结束日 = 周五(M11)
    if keys == {"week_offset", "weekday"}:
        k = _as_int(when["week_offset"], "week_offset")
        w = _as_int(when["weekday"], "weekday")
        if k not in (0, 1):
            raise ResolveReject("invalid_value", "week_offset 仅 0|1")
        if not 1 <= w <= 7:
            raise ResolveReject("invalid_value", "weekday 越界(1-7)")
        monday = _monday_of(today) + timedelta(weeks=k)
        d = monday + timedelta(days=w - 1)  # 组合 = 单日(与单 weekday"已过→下周"规则无关,F-C)
        return (d, d)
    if keys == {"start", "end"}:
        return _resolve_start_end(when, today)
    raise ResolveReject("ambiguous", f"多键/未知键 → 歧义拒绝: {sorted(keys)}")
