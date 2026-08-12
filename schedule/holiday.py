# ============================================================
# holiday.py — 中国节假日判断
# 迁入自 holiday_parser.py(批次 1),顶层旧文件留存至批次 8
# 优先级高于课表：放假 → 跳过课表查询，直接自由模式
# 数据来源：国务院办公厅《关于2026年部分节假日安排的通知》
#          国办发明电〔2025〕7号（2025-11-04发布）
# ============================================================

import sys

from datetime import datetime, date, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))

# ── 2026年节假日 ────────────────────────────────────────────
# 格式: "节日名": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}

HOLIDAYS = {
    "元旦":   {"start": "2026-01-01", "end": "2026-01-03"},
    "春节":   {"start": "2026-02-15", "end": "2026-02-23"},
    "清明节": {"start": "2026-04-04", "end": "2026-04-06"},
    "劳动节": {"start": "2026-05-01", "end": "2026-05-05"},
    "端午节": {"start": "2026-06-19", "end": "2026-06-21"},
    "中秋节": {"start": "2026-09-25", "end": "2026-09-27"},
    "国庆节": {"start": "2026-10-01", "end": "2026-10-07"},
}

# ── 调休上班日 ──────────────────────────────────────────────
# 这些日期虽然是周末，但要上课/上班

MAKEUP_WORKDAYS = {
    "2026-01-04": "元旦调休",
    "2026-02-14": "春节调休",
    "2026-02-28": "春节调休",
    "2026-05-09": "劳动节调休",
    "2026-09-20": "国庆节调休",
    "2026-10-10": "国庆节调休",
}

# ── 寒暑假（大致范围，精确到学期起止）────────────────────
# 寒假：1月中旬 ~ 2月中旬（春节前后）
# 暑假：7月初 ~ 8月底
# 这些通过 semester_start 和实际课表覆盖，此处仅作参考标记

class HolidayParser:
    """节假日判断器。先于课表查询。"""

    def __init__(self, data_path: str = None):
        self._holidays: dict[str, tuple[date, date]] = {}
        self._makeup: dict[date, str] = {}
        self._parse()

        # 支持从 JSON 文件加载（每年更新一次即可）
        # 显式 data_path（调用方已锚定 base_dir）时只读它，不再回退 cwd ——
        # 避免 cron 工作目录漂移时加载到无关目录的 holidays.json。
        # 未指定 data_path 时保留原 cwd 默认行为（CLI/测试兼容）。
        if data_path:
            if Path(data_path).exists():
                self._load_override(data_path)
            return
        default = Path("holidays.json")
        if default.exists():
            self._load_override(str(default))

    def _parse(self):
        """解析内置假期数据"""
        for name, r in HOLIDAYS.items():
            start = date.fromisoformat(r["start"])
            end = date.fromisoformat(r["end"])
            self._holidays[name] = (start, end)

        for d_str, reason in MAKEUP_WORKDAYS.items():
            self._makeup[date.fromisoformat(d_str)] = reason

    def _load_override(self, path: str):
        """从 JSON 文件加载覆盖数据（用于跨年更新）。
        跨年键策略:同名同键同年 → 覆盖(更新精确数据);同名不同年 → 按 start.year 归组追加
        (不按键覆盖,避免 2027 键把内置 2026 表冲掉);坏条目(缺 start/end 或日期非法)逐条
        try/except 跳过 + stderr 告警。"""
        import json
        p = Path(path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            print(f"[schedule.holiday] holidays.json 损坏,忽略 override 仅用内嵌: {path}", file=sys.stderr)
            return
        if not isinstance(data, dict):
            print(f"[schedule.holiday] holidays.json 顶层非 dict,忽略 override 仅用内嵌: {path}",
                  file=sys.stderr)
            return
        for name, r in data.get("holidays", {}).items():
            try:
                s = date.fromisoformat(r["start"])
                e = date.fromisoformat(r["end"])
            except (KeyError, ValueError, TypeError):
                print(f"[schedule.holiday] holidays.json 坏条目跳过(缺 start/end 或日期非法): "
                      f"{name!r}", file=sys.stderr)
                continue
            if s > e:
                print(f"[schedule.holiday] holidays.json 坏条目跳过(start > end): {name!r}",
                      file=sys.stderr)
                continue
            if name in self._holidays:
                if self._holidays[name][0].year == s.year:
                    self._holidays[name] = (s, e)
                else:
                    self._holidays[f"{name}@{s.year}"] = (s, e)   # 同名不同年 → 归组追加
            else:
                self._holidays[name] = (s, e)
        for d_str, reason in data.get("makeup_workdays", {}).items():
            try:
                self._makeup[date.fromisoformat(d_str)] = reason
            except (ValueError, TypeError):
                print(f"[schedule.holiday] holidays.json 坏调休日跳过: {d_str!r}", file=sys.stderr)

    # ── 查询 API ──────────────────────────────────────────

    def is_holiday(self, d: date | datetime) -> bool:
        """是否在法定节假日假期内"""
        if isinstance(d, datetime):
            d = d.date()
        for start, end in self._holidays.values():
            if start <= d <= end:
                return True
        return False

    def is_makeup_workday(self, d: date | datetime) -> bool:
        """是否是调休上班日（周末但要上课）"""
        if isinstance(d, datetime):
            d = d.date()
        return d in self._makeup

    def is_weekend(self, d: date | datetime) -> bool:
        """是否是普通周末（非调休）"""
        if isinstance(d, datetime):
            d = d.date()
        if d.weekday() >= 5:  # Sat=5, Sun=6
            return d not in self._makeup
        return False

    def is_school_day(self, d: date | datetime) -> bool:
        """
        今天是否应该上课。
        规则：非节假日 且（工作日 或 调休上班日）
        """
        if self.is_holiday(d):
            return False
        if isinstance(d, datetime):
            d = d.date()
        if d.weekday() >= 5 and d not in self._makeup:
            return False
        return True

    def holiday_name(self, d: date | datetime) -> str | None:
        """返回节日名称，非节假日返回 None"""
        if isinstance(d, datetime):
            d = d.date()
        for name, (start, end) in self._holidays.items():
            if start <= d <= end:
                return name.split("@", 1)[0]   # 跨年归组键剥年份后缀,返回原名
        return None

    def range_of(self, name: str) -> tuple[date, date] | None:
        """按名称查区间;未知名称返回 None。供 replan ref 校验 / resolve_scale / T2 文案同源。
        跨年归组键（name@year）按原名匹配最近年份区间——先精确匹配，再回退同名前缀取最大 s.year。"""
        hit = self._holidays.get(name)
        if hit is not None:
            return hit
        best = None
        for key, (s, e) in self._holidays.items():
            if key.split("@", 1)[0] == name and (best is None or s.year > best[0].year):
                best = (s, e)
        return best

    def all_ranges(self) -> dict[str, tuple[date, date]]:
        """合并后全部区间副本(内嵌 + json override)。"""
        return dict(self._holidays)

    def query(self, d: date | datetime) -> dict:
        """
        完整查询。
        返回:
          {
            "is_holiday": bool,
            "holiday_name": str | null,
            "is_weekend": bool,
            "is_makeup_workday": bool,
            "is_school_day": bool,
            "hint": str,          # 给模型后端的上下文提示
          }
        """
        return {
            "is_holiday": self.is_holiday(d),
            "holiday_name": self.holiday_name(d),
            "is_weekend": self.is_weekend(d),
            "is_makeup_workday": self.is_makeup_workday(d),
            "is_school_day": self.is_school_day(d),
            "hint": self._hint(d),
        }

    def _hint(self, d: date | datetime) -> str:
        if self.is_holiday(d):
            name = self.holiday_name(d)
            return f"今天是{name}假期，主人放假在家，菓菓可以发消息。"
        if self.is_makeup_workday(d):
            reason = self._makeup.get(d.date() if isinstance(d, datetime) else d, "")
            return f"今天是{reason}，虽然是周末但要上课。"
        if self.is_weekend(d):
            return "今天是周末，主人没课。"
        return "今天是普通工作日/上学日。"


# ── CLI ────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = HolidayParser()

    if len(sys.argv) > 1:
        try:
            d = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"用法: {sys.argv[0]} [YYYY-MM-DD]")
            sys.exit(1)
    else:
        d = datetime.now(CST).date()

    result = parser.query(d)
    import json
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
