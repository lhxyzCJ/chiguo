# schedule/parser.py — 课表数据面:xlsx 解析 + 缓存 + 刷新（netease bridge 模式）
# 解析 xskb.xlsx → schedule_cache.json → query(now) → 上课状态
# 零 token 消耗，确定性解析，mtime 变化自动重新解析
# 查询计算委托 schedule.query.schedule_query（纯函数）。

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from schedule.parsing import parse_cell
from schedule.query import schedule_query


class ScheduleParser:
    """课表数据面。构造签名与 .query() 返回形状完全兼容旧版（调度方零改动）。

    职责: xlsx 读取/mtime 刷新/缓存落盘;查询计算委托 schedule_query（纯函数）。"""

    def __init__(self, xlsx_path: str = "data/xskb.xlsx",
                 cache_path: str = "schedule_cache.json",
                 semester_start: date | None = None,
                 enabled: bool = True):
        if semester_start is None:
            raise ValueError("semester_start is required (config [schedule].semester_start)")
        self.xlsx_path = Path(xlsx_path)
        self.cache_path = Path(cache_path)
        self.enabled = enabled          # 可选来源开关（false → 完全不解析，query 返回空课表）
        self.available = False          # 课表数据是否可用（enabled 且有解析/缓存）
        self.semester_start = semester_start
        self._schedule: dict = {}   # {weekday: {period: course_info}}
        self._parsed_at: float = 0
        self._ensure_parsed()

    # ── 解析 ──────────────────────────────────────────────

    def _ensure_parsed(self):
        """如果 xlsx 被更新，重新解析"""
        if not self.enabled:
            return
        self._load_cache()
        if not self.xlsx_path.exists():
            # xlsx 缺失时保留缓存课表（缓存不存在则保持空课表）
            return
        xlsx_mtime = self.xlsx_path.stat().st_mtime
        if xlsx_mtime <= self._parsed_at:
            return  # 缓存新鲜，直接复用
        if self._parse():
            self._parsed_at = xlsx_mtime
            self.available = True
            self._save_cache()
        else:
            # 解析失败（xlsx 损坏/openpyxl 缺失等）：降级空课表，但保留旧缓存
            # （不用空数据覆盖落盘）。记住 mtime 避免每轮 query 重复解析刷 stderr，
            # xlsx 修复后 mtime 变化会自然重试。
            self._parsed_at = xlsx_mtime

    def _parse(self) -> bool:
        """解析 xlsx，提取每节课信息。失败（openpyxl 缺失/文件损坏/空表）降级空课表，不崩溃。
        返回 bool：True = 解析成功（调用方应落盘缓存）；False = 失败（保留旧缓存）。"""
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(self.xlsx_path))
            if not wb.sheetnames:
                raise ValueError("xlsx has no sheets")
            ws = wb[wb.sheetnames[0]]
        except Exception as e:
            print(f"[schedule_parser] xlsx parse failed ({e}), schedule=empty", file=sys.stderr)
            self._schedule = {}
            return False

        self._schedule = {}
        weekday_map = {i: i - 1 for i in range(1, 8)}  # col 2→Mon(0), col 8→Sun(6)

        for row in ws.iter_rows(min_row=5, max_row=15, values_only=True):  # 第5行起是课表数据
            if not row or not row[0]:
                continue
            try:
                period = int(float(str(row[0])))
            except (ValueError, TypeError):
                continue
            if period < 1 or period > 11:
                continue

            for col_idx in range(1, 8):  # 周一~周日
                if col_idx >= len(row):
                    continue
                cell = str(row[col_idx]).strip() if row[col_idx] else ""
                if not cell or cell == "None":
                    continue

                courses = parse_cell(cell)
                if not courses:
                    continue

                weekday = col_idx - 1  # 0=Mon ... 6=Sun
                if weekday not in self._schedule:
                    self._schedule[weekday] = {}
                if len(courses) == 1:
                    self._schedule[weekday][period] = courses[0]
                else:
                    # 合并单元格：同一时段多门课（周次互斥，如 2-17 与 19 周）。
                    # 主课程存原字段，其余存入 alternates
                    self._schedule[weekday][period] = {
                        **courses[0], "alternates": courses[1:]
                    }
        return True

    # ── 缓存 ──────────────────────────────────────────────

    def _save_cache(self):
        def make_serializable(c):
            c = {**c, "weeks": sorted(c["weeks"])}
            c["alternates"] = [
                {**a, "weeks": sorted(a["weeks"])}
                for a in (c.get("alternates") or [])
            ]
            return c

        data = {
            "cache_version": 2,
            "parsed_at": self._parsed_at,
            "schedule": {
                str(day): {
                    str(period): make_serializable(course)
                    for period, course in periods.items()
                }
                for day, periods in self._schedule.items()
            }
        }
        tmp_path = Path(str(self.cache_path) + ".tmp")
        tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        os.replace(tmp_path, self.cache_path)

    def _load_cache(self):
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
            if not isinstance(data, dict):
                raise ValueError("cache root must be a dict")
            parsed_at = data.get("parsed_at", 0)
            schedule = {}
            for day_str, periods in data.get("schedule", {}).items():
                day = int(day_str)
                schedule[day] = {}
                for period_str, course in periods.items():
                    c = dict(course)
                    c["weeks"] = set(c.get("weeks", []))  # list → set
                    # v2: 合并单元格的备选课程（旧缓存无此字段 → 空列表）
                    c["alternates"] = [
                        {**a, "weeks": set(a.get("weeks", []))}
                        for a in (c.get("alternates") or [])
                    ]
                    schedule[day][int(period_str)] = c
        except (json.JSONDecodeError, ValueError, TypeError, AttributeError, OSError):
            # 缓存损坏：删除坏文件并忽略，避免 daemon 崩溃
            try:
                self.cache_path.unlink()
            except OSError:
                pass
            return
        self._parsed_at = parsed_at
        self._schedule = schedule
        self.available = True
        # v2: 旧版本缓存（合并单元格课被吞进 location）→ 强制重解析（xlsx 存在时）
        if data.get("cache_version", 1) < 2:
            self._parsed_at = 0

    # ── 查询 ──────────────────────────────────────────────

    def query(self, now: datetime) -> dict:
        """兼容入口:先刷新,再委托纯函数计算。返回形状与旧版逐键相同。"""
        self._ensure_parsed()
        result = schedule_query(self._schedule, self.semester_start, now)
        result["available"] = self.available
        return result


# ── CLI ────────────────────────────────────────────────────

if __name__ == "__main__":
    from datetime import timezone, timedelta
    CST = timezone(timedelta(hours=8))

    try:
        import tomllib
        with open("chiguo_proactive.toml", "rb") as _f:
            _cfg = tomllib.load(_f)
        _sem = date.fromisoformat(_cfg["schedule"]["semester_start"])
    except Exception as e:
        print(f"[schedule_parser] 读取 chiguo_proactive.toml [schedule].semester_start 失败: {e}",
              file=sys.stderr)
        sys.exit(2)

    parser = ScheduleParser("data/xskb.xlsx", semester_start=_sem)

    if len(sys.argv) > 1 and sys.argv[1] == "--dump":
        print(json.dumps(parser._schedule, indent=2, ensure_ascii=False, default=str))
    else:
        now = datetime.now(CST)
        result = parser.query(now)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
