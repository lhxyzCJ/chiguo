# ============================================================
# schedule/recall.py — 回忆接口(§4.3):日期/关键词检索登记事实,偏移引擎算好。
# 日期查询 ±7 天窗口;关键词全量子串扫描(不截窗口,F10);无匹配 → no_match 显式信号。
# ============================================================

import re
from datetime import date, timedelta

_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_CN_DATE_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日$")

MAX_MATCHES = 20


def _parse_query_date(q: str, today: date) -> date | None:
    m = _DATE_RE.match(q)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = _CN_DATE_RE.match(q)
    if m:
        mm, dd = int(m[1]), int(m[2])
        try:
            d = date(today.year, mm, dd)
        except ValueError:
            try:
                return date(today.year + 1, mm, dd)
            except ValueError:
                return None
        return d if d >= today else date(today.year + 1, mm, dd)
    return None


def _in_window(day: date, target: date, half: int = 7) -> bool:
    return abs((day - target).days) <= half


def _by_date(d: date, sources, today: date) -> list[dict]:
    matches = []
    for a in sources.anniversaries.visible_items():
        if a.get("type") != "anniversary":
            continue
        try:
            from schedule.anniversary import mmdd_to_date
            ad = mmdd_to_date(a["date"], d.year)   # 按查询日 d 的年份解析(跨年窗口不漏报)
        except ValueError:
            continue
        if _in_window(ad, d):
            matches.append({"type": "anniversary", "date": a["date"], "label": a["name"],
                            "days_until": (ad - today).days})
    lo, hi = d - timedelta(days=7), d + timedelta(days=7)
    for r in sources.overrides.reminders_in(lo, hi):
        rd = date.fromisoformat(r["date"])
        matches.append({"type": "reminder", "date": r["date"], "label": r["label"],
                        "days_until": (rd - today).days})
    for ov in sources.overrides.items():
        if ov["kind"] == "reminder":
            continue
        s = date.fromisoformat(ov["date"])
        e = date.fromisoformat(ov.get("end_date") or ov["date"])
        if s <= hi and e >= lo:   # 区间重叠 ±7
            label = ov.get("label") or ov.get("note") or (ov.get("course") or {}).get("course", "")
            matches.append({"type": "override", "date": ov["date"], "label": label})
    for name, (s, e) in sources.holiday.all_ranges().items():
        if s <= hi and e >= lo:
            matches.append({"type": "holiday", "date": s.isoformat(), "label": name})
    if sources.break_state:
        for b in sources.break_state.get("breaks", []):
            try:
                s, e = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if s <= hi and e >= lo:
                matches.append({"type": "break", "date": b["start"], "label": b.get("note", "寒暑假")})
    return matches


def _by_keyword(q: str, sources, today: date) -> list[dict]:
    """全量子串扫描(不截窗口,F10):纪念日 name / 提醒日 label / 例外 note/course/exam_week label(F11)。"""
    ql = q.lower()
    matches = []
    for a in sources.anniversaries.visible_items():
        if a.get("type") == "anniversary" and ql in a["name"].lower():
            try:
                from schedule.anniversary import mmdd_to_date
                ad = mmdd_to_date(a["date"], today.year)
                if ad < today:
                    ad = mmdd_to_date(a["date"], today.year + 1)
            except ValueError:
                continue
            matches.append({"type": "anniversary", "date": a["date"], "label": a["name"],
                            "days_until": (ad - today).days})
    for ov in sources.overrides.items():
        label = ov.get("label") or ov.get("note") or (ov.get("course") or {}).get("course", "")
        if ql in label.lower():
            matches.append({"type": "override", "date": ov["date"], "label": label})
    for name in sources.holiday.all_ranges():
        if ql in name.lower():
            matches.append({"type": "holiday", "date": sources.holiday.range_of(name)[0].isoformat(),
                            "label": name})
    return matches


def recall(query, sources, today: date, max_matches: int = MAX_MATCHES) -> dict:
    q = str(query).strip()
    if not q:
        return {"query": q, "matches": [], "no_match": True}
    d = _parse_query_date(q, today)
    matches = _by_date(d, sources, today) if d is not None else _by_keyword(q, sources, today)
    return {"query": q, "matches": matches[:max_matches], "no_match": not matches}
