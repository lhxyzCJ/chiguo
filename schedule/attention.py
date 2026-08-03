# ============================================================
# schedule/attention.py — 注意力分层组装(§5.4):T1 重要日子/T2 区间事实/T3 课表周窗口
# 纯函数零 I/O;--attention(批 5)与内容层注入共用同源组装。
# ============================================================

from datetime import date

from schedule.anniversary import mmdd_to_date
from schedule.day_plan import week_number, week_courses, _on_break


def t1_items(sources, today: date, t1_max: int = 50) -> list[dict]:
    """纪念日/生日:当年全部(今天 ~ 12-31,已过自然退出,次年同日重新进入);
    提醒日:一次性(date ∈ [today, 12-31],跨年排除);合并按 days_until 排序,截断 ≤50。"""
    items = []
    for a in sources.anniversaries.visible_items():
        if a.get("type") != "anniversary":
            continue
        try:
            d = mmdd_to_date(a["date"], today.year)
        except ValueError:
            continue
        if today <= d <= date(today.year, 12, 31):
            items.append({"kind": "anniversary", "name": a["name"], "date": d.isoformat(),
                          "days_until": (d - today).days})
    end = date(today.year, 12, 31)
    for r in sources.overrides.reminders_in(today, end):
        d = date.fromisoformat(r["date"])
        items.append({"kind": "reminder", "label": r["label"], "date": r["date"],
                      "days_until": (d - today).days})
    items.sort(key=lambda x: (x["days_until"], x["date"]))
    return items[:t1_max]


def t2_block(sources, today: date, horizon: int = 14) -> list[str]:
    """生效中 + 未来 14 天区间事实(节假日/寒暑假/考试周)文案。
    第X天公式钉死:(today - start).days + 1,起始日 = 第 1 天;还有X天 = (start - today).days。
    manual break(无日期区间)→ 仅"寒暑假模式中"(第X天公式不可算,N6)。"""
    lines = []
    for name, (s, e) in sources.holiday.all_ranges().items():
        if s <= today <= e:
            lines.append(f"今天是放{name}第{(today - s).days + 1}天")
        elif s > today and (s - today).days <= horizon:
            lines.append(f"还有{(s - today).days}天放{name}")
    for it in sources.overrides.intervals():
        s = date.fromisoformat(it["date"])
        e = date.fromisoformat(it.get("end_date") or it["date"])
        label = it.get("label", "考试周")
        if s <= today <= e:
            lines.append(f"今天是{label}第{(today - s).days + 1}天")
        elif s > today and (s - today).days <= horizon:
            lines.append(f"还有{(s - today).days}天{label}")
    if _on_break(sources.break_state, sources.semester_end, today):
        bs = sources.break_state or {}
        if bs.get("manual_override") or bs.get("on_break"):
            lines.append("寒暑假模式中")
        else:
            for b in bs.get("breaks", []):
                try:
                    s, e = date.fromisoformat(b["start"]), date.fromisoformat(b["end"])
                except (KeyError, ValueError, TypeError):
                    continue
                if s <= today <= e:
                    lines.append(f"今天是放假第{(today - s).days + 1}天")
                    break
    return lines


def t3_window(sources, today: date) -> dict:
    """本周 + 下周(周日滚动):week_courses(w) ∪ week_courses(w+1)。"""
    w = week_number(today, sources.semester_start)
    return {"current_week": w,
            "this_week": week_courses(sources.schedule, sources.semester_start, w),
            "next_week": week_courses(sources.schedule, sources.semester_start, w + 1)}


def today_exceptions(sources, today: date) -> list[dict]:
    """当日课程例外摘要(§5.4 --attention):[{period, action, course?, date}];
    move 源日 period=源节次,目标日 period=to_period。"""
    out = []
    for ov in sorted(sources.overrides.for_date(today), key=lambda x: x.get("created_at", "")):
        kind = ov["kind"]
        if kind not in ("cancel", "move", "add"):
            continue
        if kind == "move":
            to_d = ov.get("to_date")
            if to_d and to_d != today.isoformat():
                e = {"period": ov.get("period"), "action": "move", "date": ov["date"]}
            else:
                e = {"period": ov.get("to_period"), "action": "move", "date": ov["date"]}
        else:
            e = {"period": ov.get("period"), "action": kind, "date": ov["date"]}
        if ov.get("course"):
            e["course"] = ov["course"].get("course", "")
        out.append(e)
    return out


def build_attention(sources, today: date, t1_max: int = 50) -> dict:
    return {"t1": t1_items(sources, today, t1_max), "t2": t2_block(sources, today),
            "t3": t3_window(sources, today), "week_num": week_number(today, sources.semester_start),
            "today_exceptions": today_exceptions(sources, today)}
