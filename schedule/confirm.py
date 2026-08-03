# ============================================================
# schedule/confirm.py — 确定性确认文案 + H5 拒绝类别映射(二十轮 A2 钉死)
# 生成者 = 确定性模板(引用已登记条目原文 + 星期数 + 日期,L1),非提取 agent。
# 供 daemon --schedule-change 分支(Task 11)调用;bridge 零解析直取 text/question。
# ============================================================

from datetime import date

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def fmt_date(d: date) -> str:
    return f"{d.month}月{d.day}日{WEEKDAY_NAMES[d.weekday()]}"


def build_confirmation(item: dict) -> str:
    """成功确认文案:条目原文 + 星期数+日期。kind 全覆盖。"""
    d = date.fromisoformat(item["date"])
    ds = fmt_date(d)
    kind = item["kind"]
    if kind == "cancel":
        name = (item.get("course") or {}).get("course", "课") if item.get("course") else "课"
        return f"好,{ds}第{item['period']}节的{name}停课,记下了。"
    if kind == "move":
        name = (item.get("course") or {}).get("course", "课")
        ts = fmt_date(date.fromisoformat(item["to_date"])) if item.get("to_date") else ds
        if item.get("period") is None:
            return f"好,{ds}的{name}调到{ts}第{item['to_period']}节,记下了。"
        return f"好,{ds}第{item['period']}节的{name}调到{ts}第{item['to_period']}节,记下了。"
    if kind == "add":
        name = (item.get("course") or {}).get("course", "课")
        return f"好,{ds}第{item['period']}节加了一节{name},记下了。"
    if kind == "exam_week":
        es = fmt_date(date.fromisoformat(item["end_date"])) if item.get("end_date") else ds
        label = item.get("label", "考试周")
        return f"好,{label}是{ds}到{es},记下了。"
    if kind == "reminder":
        return f"好,{ds}要{item['label']},我记着。"
    return f"好,记下了。"


# H5 拒绝类别 → (追问文案, missing 字段)(spec §6,二十轮补钉)
H5_TEMPLATES = {
    "past_date":        ("这个日期已经过去了,告诉哥哥具体哪天的安排", ["date"]),
    "ambiguous":        ("哥哥没太听明白,再告诉哥哥一次具体安排?", ["when"]),
    "invalid_value":    ("哥哥没太听明白,再告诉哥哥一次具体安排?", ["when"]),
    "shape_mismatch":   ("这个安排有点对不上,哥哥再确认一下?", ["period"]),
    "before_semester":  ("现在还没到开学,告诉哥哥具体日期?", ["date"]),
    "after_semester":   ("学期已经结束了,告诉哥哥具体日期?", ["date"]),
    "no_source_class":  ("那天那节没有课哦,再告诉哥哥一次?", ["period"]),
    "not_found":        ("没找到这条安排,哥哥再确认一下?", ["date"]),
}


def build_question(category: str) -> tuple[str, list[str]]:
    """拒绝类别 → (追问文案, missing)。未知类别回退 ambiguous。"""
    return H5_TEMPLATES.get(category, H5_TEMPLATES["ambiguous"])
