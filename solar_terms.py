# ============================================================
# solar_terms.py — 24节气日期查询（按年动态消费 · 单一事实源）
# 零依赖。不再硬编码任何年份的节气表。
# 日期权威单一事实源 = update_holidays.get_solar_terms_for(year)：
#   - 2026/2027 为天文权威校准精确表；
#   - 其余年份由跨年估算算法动态推导。
# 本模块只做"按年取表 + 就近窗口命中"，hint 文案随表单源流出。
# ============================================================

from datetime import date, timedelta

from update_holidays import get_solar_terms_for


class SolarTerms:
    """24节气查询。按年由单一事实源动态生成，纯日期计算，零依赖。"""

    def __init__(self):
        # year -> 当年 24 节气表（含 name/month/day/hint）
        self._cache: dict[int, list] = {}

    def terms_for(self, year: int) -> list:
        """返回指定年份的 24 节气表（含 hint）。带按年缓存。"""
        if year not in self._cache:
            self._cache[year] = list(get_solar_terms_for(year))
        return self._cache[year]

    def get_term(self, year: int, month: int, day: int) -> dict | None:
        """精确匹配某年某月某日是否为节气（跨年动态表）。"""
        for t in self.terms_for(year):
            if t["month"] == month and t["day"] == day:
                return dict(t)
        return None

    def nearby_term(self, d: date, window_days: int = 1) -> dict | None:
        """
        检查 d ± window_days 范围内是否接近某个节气。
        每个被检查日期按其自身的年份取动态节气表，自动处理年边界。
        返回距离最近的节气 dict，无匹配返回 None。
        """
        best = None
        best_dist = window_days + 1  # 初始值比最大窗口还大

        for offset in range(-window_days, window_days + 1):
            check = d + timedelta(days=offset)
            term = self.get_term(check.year, check.month, check.day)
            if term:
                dist = abs(offset)
                if dist < best_dist:
                    best = dict(term)
                    best["_match_date"] = check.isoformat()
                    best["_days_offset"] = offset
                    best_dist = dist

        return best
