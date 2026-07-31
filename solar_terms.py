# ============================================================
# solar_terms.py — 24节气日期查询
# 零依赖。2026年近似日期硬编码，±1天窗口。
# 数据来源：太阳黄经15°整数倍对应的日期（2026年近似值）
# ============================================================

import json
from datetime import date, timedelta
from pathlib import Path

# ── 24节气（2026年近似日期）─────────────────────────────────
# 实际日期可能随年份有±1天偏差，话题注入用近似值即可。

SOLAR_TERMS = [
    {"name": "小寒", "month": 1,  "day": 5,  "hint": "小寒到了，一年中最冷的时候，关心主人保暖"},
    {"name": "大寒", "month": 1,  "day": 20, "hint": "大寒了，注意防寒保暖，出门多穿点"},
    {"name": "立春", "month": 2,  "day": 4,  "hint": "立春了~问问主人有没有吃春饼或春卷"},
    {"name": "雨水", "month": 2,  "day": 19, "hint": "雨水节气，关心主人出门有没有带伞"},
    {"name": "惊蛰", "month": 3,  "day": 5,  "hint": "惊蛰了，提醒主人注意春雷和换季"},
    {"name": "春分", "month": 3,  "day": 20, "hint": "春分到了，昼夜等长，问问主人最近状态怎么样"},
    {"name": "清明", "month": 4,  "day": 5,  "hint": "清明时节，关心主人有没有去扫墓或踏青"},
    {"name": "谷雨", "month": 4,  "day": 20, "hint": "谷雨了，春雨绵绵，关心主人心情"},
    {"name": "立夏", "month": 5,  "day": 5,  "hint": "立夏了~问问主人有没有吃立夏饭"},
    {"name": "小满", "month": 5,  "day": 21, "hint": "小满到了，天气渐热，关心主人注意防暑"},
    {"name": "芒种", "month": 6,  "day": 5,  "hint": "芒种了，提醒主人注意天气变化"},
    {"name": "夏至", "month": 6,  "day": 21, "hint": "夏至到了，是一年中白天最长的一天"},
    {"name": "小暑", "month": 7,  "day": 7,  "hint": "小暑到了，天气开始炎热，关心主人防暑"},
    {"name": "大暑", "month": 7,  "day": 22, "hint": "大暑了，一年中最热的时候，提醒主人多喝水"},
    {"name": "立秋", "month": 8,  "day": 7,  "hint": "立秋了~主人吃饺子了吗"},
    {"name": "处暑", "month": 8,  "day": 23, "hint": "处暑了，暑气渐消，关心主人身体"},
    {"name": "白露", "month": 9,  "day": 7,  "hint": "白露到了，早晚温差大，提醒主人添衣"},
    {"name": "秋分", "month": 9,  "day": 23, "hint": "秋分了，昼夜平分，关心主人最近过得怎么样"},
    {"name": "寒露", "month": 10, "day": 8,  "hint": "寒露到了，天气转凉，关心主人保暖"},
    {"name": "霜降", "month": 10, "day": 23, "hint": "霜降了，深秋时节，提醒主人注意保暖"},
    {"name": "立冬", "month": 11, "day": 7,  "hint": "立冬了~问问主人有没有吃饺子"},
    {"name": "小雪", "month": 11, "day": 22, "hint": "小雪到了，天冷添衣，关心主人"},
    {"name": "大雪", "month": 12, "day": 7,  "hint": "大雪了，注意保暖防寒"},
    {"name": "冬至", "month": 12, "day": 21, "hint": "冬至了~问问主人吃饺子还是汤圆"},
]


class SolarTerms:
    """24节气查询。纯日期计算，零依赖。"""

    def __init__(self, data_path: str = None):
        self._terms = list(SOLAR_TERMS)
        for path in ([data_path] if data_path else []) + ["solar_terms.json"]:
            p = Path(path)
            if p.exists():
                try:
                    loaded = json.loads(p.read_text())
                    if isinstance(loaded, list) and len(loaded) > 0:
                        self._terms = loaded
                    break
                except Exception:
                    pass

    def get_term(self, month: int, day: int) -> dict | None:
        """精确匹配某月某日是否为节气。"""
        for t in self._terms:
            if t["month"] == month and t["day"] == day:
                return dict(t)
        return None

    def nearby_term(self, d: date, window_days: int = 1) -> dict | None:
        """
        检查 d ± window_days 范围内是否接近某个节气。
        返回距离最近的节气 dict，无匹配返回 None。
        Python date 加减 timedelta 自动处理年边界。
        """
        best = None
        best_dist = window_days + 1  # 初始值比最大窗口还大

        for offset in range(-window_days, window_days + 1):
            check = d + timedelta(days=offset)
            term = self.get_term(check.month, check.day)
            if term:
                dist = abs(offset)
                if dist < best_dist:
                    best = dict(term)
                    best["_match_date"] = check.isoformat()
                    best["_days_offset"] = offset
                    best_dist = dist

        return best
