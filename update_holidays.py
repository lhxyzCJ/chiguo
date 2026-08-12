#!/usr/bin/env python3
"""
update_holidays.py — 节假日数据更新脚本

生成指定年份的 holidays.json 和 solar_terms.json。
优先尝试 chinese_calendar 包（精确），否则用内置估算值。

用法:
  python3 update_holidays.py 2027              # 生成 2027 holidays.json
  python3 update_holidays.py 2027 --solar      # 同时生成节气数据
  python3 update_holidays.py 2027 --force      # 覆盖同年数据(保留其它年份)
"""

import json
import os
import sys
from datetime import date
from pathlib import Path


# 锚定到脚本所在目录，避免从其他 cwd 运行时写到别处
BASE_DIR = Path(__file__).resolve().parent


# ═══════════════════════════════════════════════════════════
# 内置估算（国务院通知发布前使用，每年 11 月手册更新此表）
# ═══════════════════════════════════════════════════════════

# 已知年份的精确数据（从国务院通知摘录）
KNOWN_HOLIDAYS = {
    2026: {
        "元旦":   ("2026-01-01", "2026-01-03"),
        "春节":   ("2026-02-15", "2026-02-23"),
        "清明节": ("2026-04-04", "2026-04-06"),
        "劳动节": ("2026-05-01", "2026-05-05"),
        "端午节": ("2026-06-19", "2026-06-21"),
        "中秋节": ("2026-09-25", "2026-09-27"),
        "国庆节": ("2026-10-01", "2026-10-07"),
    },
}

KNOWN_MAKEUP = {
    2026: {
        "2026-01-04": "元旦调休",
        "2026-02-14": "春节调休",
        "2026-02-28": "春节调休",
        "2026-05-09": "劳动节调休",
        "2026-09-20": "国庆节调休",
        "2026-10-10": "国庆节调休",
    },
}

# 2027 估算（基于太阳/农历规律，需国务院通知确认）
# 春节 ≈ 农历正月初一。2027 年春节约为 2 月 6 日（2027 = 丁未年）。
# 实际日期以国务院通知为准。
ESTIMATED_2027 = {
    "元旦":   ("2027-01-01", "2027-01-03"),
    "春节":   ("2027-02-06", "2027-02-14"),
    "清明节": ("2027-04-04", "2027-04-06"),
    "劳动节": ("2027-05-01", "2027-05-05"),
    "端午节": ("2027-06-09", "2027-06-11"),
    "中秋节": ("2027-09-15", "2027-09-17"),
    "国庆节": ("2027-10-01", "2027-10-07"),
}

# 24 节气（按太阳黄经 15° 近似，每年 ±1 天）
# 2027 估算值
SOLAR_TERMS_2027 = [
    {"name": "小寒", "month": 1,  "day": 5,  "hint": "小寒到了，一年中最冷的时候，关心主人保暖"},
    {"name": "大寒", "month": 1,  "day": 20, "hint": "大寒了，注意防寒保暖，出门多穿点"},
    {"name": "立春", "month": 2,  "day": 4,  "hint": "立春了~问问主人有没有吃春饼或春卷"},
    {"name": "雨水", "month": 2,  "day": 19, "hint": "雨水节气，关心主人出门有没有带伞"},
    {"name": "惊蛰", "month": 3,  "day": 6,  "hint": "惊蛰了，提醒主人注意春雷和换季"},
    {"name": "春分", "month": 3,  "day": 21, "hint": "春分到了，昼夜等长，问问主人最近状态怎么样"},
    {"name": "清明", "month": 4,  "day": 5,  "hint": "清明时节，关心主人有没有去扫墓或踏青"},
    {"name": "谷雨", "month": 4,  "day": 20, "hint": "谷雨了，春雨绵绵，关心主人心情"},
    {"name": "立夏", "month": 5,  "day": 6,  "hint": "立夏了~问问主人有没有吃立夏饭"},
    {"name": "小满", "month": 5,  "day": 21, "hint": "小满到了，天气渐热，关心主人注意防暑"},
    {"name": "芒种", "month": 6,  "day": 6,  "hint": "芒种了，提醒主人注意天气变化"},
    {"name": "夏至", "month": 6,  "day": 21, "hint": "夏至到了，是一年中白天最长的一天"},
    {"name": "小暑", "month": 7,  "day": 7,  "hint": "小暑到了，天气开始炎热，关心主人防暑"},
    {"name": "大暑", "month": 7,  "day": 23, "hint": "大暑了，一年中最热的时候，提醒主人多喝水"},
    {"name": "立秋", "month": 8,  "day": 7,  "hint": "立秋了~主人吃饺子了吗"},
    {"name": "处暑", "month": 8,  "day": 23, "hint": "处暑了，暑气渐消，关心主人身体"},
    {"name": "白露", "month": 9,  "day": 8,  "hint": "白露到了，早晚温差大，提醒主人添衣"},
    {"name": "秋分", "month": 9,  "day": 23, "hint": "秋分了，昼夜平分，关心主人最近过得怎么样"},
    {"name": "寒露", "month": 10, "day": 8,  "hint": "寒露到了，天气转凉，关心主人保暖"},
    {"name": "霜降", "month": 10, "day": 23, "hint": "霜降了，深秋时节，提醒主人注意保暖"},
    {"name": "立冬", "month": 11, "day": 7,  "hint": "立冬了~问问主人有没有吃饺子"},
    {"name": "小雪", "month": 11, "day": 22, "hint": "小雪到了，天冷添衣，关心主人"},
    {"name": "大雪", "month": 12, "day": 7,  "hint": "大雪了，注意保暖防寒"},
    {"name": "冬至", "month": 12, "day": 22, "hint": "冬至了~问问主人吃饺子还是汤圆"},
]


# ═══════════════════════════════════════════════════════════
# 核心逻辑
# ═══════════════════════════════════════════════════════════

def try_chinese_calendar(year: int):
    """尝试用 chinese-calendar 包获取精确假期数据。失败返回 None。"""
    try:
        from chinese_calendar import get_holidays
        holidays = get_holidays(year)
        result = {}
        # chinese_calendar API varies by version: may return dict or list;
        # Holiday namedtuple may have .date/.name or .start_date/.end_date
        def _iso(x):
            return x.isoformat() if hasattr(x, "isoformat") else str(x)

        if isinstance(holidays, dict):
            for d, v in holidays.items():
                if isinstance(v, str):
                    # 旧 API: {date: "节日名"} → 单日,键名取 value
                    name, start, end = v, d, d
                else:
                    # 新 API: {date: Holiday}。name 缺失回退 key;
                    # start/end 优先取区间字段,否则单日,再否则回退 key
                    name = getattr(v, "name", None) or d
                    start = (getattr(v, "start_date", None)
                             or getattr(v, "date", None) or d)
                    end = (getattr(v, "end_date", None)
                           or getattr(v, "date", None) or d)
                # 真实 chinese_calendar 对春节/国庆等多日假期每天一条同名条目 →
                # 按名聚合 min(start)/max(end)，避免逐日覆盖塌缩为最后一天
                key = str(name)
                if key in result:
                    old_start, old_end = result[key]
                    result[key] = (min(old_start, _iso(start)), max(old_end, _iso(end)))
                else:
                    result[key] = (_iso(start), _iso(end))
        else:
            for h in holidays:
                name = str(getattr(h, 'name', h))
                # try .start_date first (old API), fallback to .date (new API)
                start = (getattr(h, 'start_date', None) or getattr(h, 'date', None))
                end = (getattr(h, 'end_date', None) or getattr(h, 'date', None))
                if start and end:
                    result[name] = (start.isoformat(), end.isoformat())
        return result if result else None
    except Exception:
        return None


def get_holidays_for(year: int) -> dict:
    """获取指定年份假期数据。已知 > 估算 > chinese_calendar。"""
    if year in KNOWN_HOLIDAYS:
        return dict(KNOWN_HOLIDAYS[year])
    cc = try_chinese_calendar(year)
    if cc:
        return cc
    if year == 2027:
        return dict(ESTIMATED_2027)
    # 通用估算：使用 2027 日期模板。固定日期假期（元旦/劳动节/国庆/清明）准确；
    # 农历假期（春节/端午/中秋）每年提前约 11 天（农历年比公历年短约 11 天：
    # 2027 春节 2/6 → 2028 春节 1/26），需等国务院通知更新 KNOWN_HOLIDAYS。
    print(f"⚠️ [update_holidays] 警告: {year} 年假期数据为通用估算值"
          f"(仅 2026/2027 有精确或专用估算),农历假期按每年约 11 天偏移推算,"
          f"请以国务院通知为准!", file=sys.stderr)
    from datetime import timedelta
    LUNAR = {"春节", "端午节", "中秋节"}
    offset = (2027 - year) * 11  # ~11 days/year lunar drift;农历年比公历年短 → 春节逐年提前
    result = {}
    for name, (start, end) in ESTIMATED_2027.items():
        sy, sm, sd = map(int, start.split("-"))
        ey, em, ed = map(int, end.split("-"))
        s_date = date(year, sm, sd)
        e_date = date(year, em, ed)
        if name in LUNAR:
            s_date += timedelta(days=offset)
            e_date += timedelta(days=offset)
        result[name] = (s_date.isoformat(), e_date.isoformat())
    return result


def get_makeup_for(year: int) -> dict:
    """获取调休日（仅已知年份有数据）。"""
    return dict(KNOWN_MAKEUP.get(year, {}))


def get_solar_terms_for(year: int) -> list:
    """获取指定年份节气。2027 有估算，其他年份基于 2027 偏移。"""
    if year == 2027:
        return list(SOLAR_TERMS_2027)
    if year == 2026:
        return None  # 内置已有
    offset_days = round((year - 2027) * 0.25)  # ~6h/year drift
    terms = []
    for t in SOLAR_TERMS_2027:
        term = dict(t)
        base = date(2027, term['month'], term['day'])
        shifted = base + timedelta(days=offset_days)
        term['month'] = shifted.month
        term['day'] = shifted.day
        terms.append(term)
    return terms


def _file_covers_year(data, year: int) -> bool:
    """holidays.json 是否已含 year 年数据:任一假期 start 落该年,或 _generated_for 匹配(R22)。"""
    if not isinstance(data, dict):
        return False
    if str(data.get("_generated_for", "")) == str(year):
        return True
    for r in (data.get("holidays") or {}).values():
        if isinstance(r, dict) and r.get("start"):
            try:
                if date.fromisoformat(str(r["start"])).year == year:
                    return True
            except ValueError:
                continue
    return False


def _merge_holidays(existing: dict, year: int, new_data: dict) -> dict:
    """跨年合并(与 schedule/holiday.py _load_override 归组语义一致,R22):
    同名同年 → 覆盖(更新精确数据);同名不同年 → 归组 name@year 键追加;新名 → 追加。"""
    merged = dict(existing)
    for name, item in new_data.items():
        prev = merged.get(name)
        if prev is None:
            merged[name] = item
            continue
        try:
            same_year = date.fromisoformat(prev["start"]).year == year
        except (KeyError, ValueError, TypeError):
            same_year = False
        merged[name if same_year else f"{name}@{year}"] = item
    return merged


def generate(year: int, force: bool = False, with_solar: bool = False):
    """主入口：生成 JSON 文件（写到脚本所在目录，与 cwd 无关）。"""
    holidays_path = BASE_DIR / "holidays.json"
    solar_path = BASE_DIR / "solar_terms.json"

    holidays = get_holidays_for(year)
    makeup = get_makeup_for(year)
    is_estimated = year not in KNOWN_HOLIDAYS

    holiday_data = {
        "holidays": {name: {"start": s, "end": e} for name, (s, e) in holidays.items()},
        "makeup_workdays": dict(makeup),
    }
    if is_estimated:
        holiday_data["_note"] = (
            f"⚠ 估算值（{year} 年国务院通知尚未发布）。"
            "请根据官方通知修正日期和调休安排。"
        )
        holiday_data["_generated_for"] = str(year)

    # 写入 holidays.json
    if holidays_path.exists():
        try:
            existing = json.loads(holidays_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            existing = None
        if _file_covers_year(existing, year) and not force:
            print(f"❌ {holidays_path} 已含 {year} 年数据。用 --force 覆盖。")
        elif isinstance(existing, dict):
            # 跨年自动合并(force 亦仅覆盖同年,保留旧年份;loader 按 name@year 归组,R22)
            merged = dict(existing)
            merged["holidays"] = _merge_holidays(
                existing.get("holidays") or {}, year, holiday_data["holidays"])
            makeup = dict(existing.get("makeup_workdays") or {})
            for d_str, reason in holiday_data["makeup_workdays"].items():
                makeup.setdefault(d_str, reason)
            merged["makeup_workdays"] = makeup
            if is_estimated:
                merged["_note"] = holiday_data.get("_note")
                merged["_generated_for"] = str(year)
            tmp = Path(str(holidays_path) + ".tmp")
            tmp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, holidays_path)
            print(f"✅ {holidays_path} 已合并 {year} 年数据(保留旧年份)")
        elif force:
            # 文件不可解析 + --force:无旧数据可保留,直接覆盖
            tmp = Path(str(holidays_path) + ".tmp")
            tmp.write_text(json.dumps(holiday_data, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, holidays_path)
            tag = "⚠ 估算" if is_estimated else "✅ 精确"
            print(f"{tag} {holidays_path} 已生成 ({year} 年, {len(holidays)} 个假期)")
        else:
            print(f"❌ {holidays_path} 已存在(不可解析)。用 --force 覆盖。")
    else:
        tmp = Path(str(holidays_path) + ".tmp")
        tmp.write_text(json.dumps(holiday_data, indent=2, ensure_ascii=False) + "\n")
        os.replace(tmp, holidays_path)
        tag = "⚠ 估算" if is_estimated else "✅ 精确"
        print(f"{tag} {holidays_path} 已生成 ({year} 年, {len(holidays)} 个假期)")

    # 节气
    if with_solar:
        terms = get_solar_terms_for(year)
        if terms:
            if solar_path.exists() and not force:
                print(f"❌ {solar_path} 已存在。用 --force 覆盖。")
            else:
                solar_path.write_text(
                    json.dumps(terms, indent=2, ensure_ascii=False) + "\n"
                )
                print(f"📅 {solar_path} 已生成 ({len(terms)} 个节气)")
        else:
            print(f"{year} 年节气已内置，无需生成 solar_terms.json")


# ═══════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(
        description="生成指定年份的节假日/节气数据文件"
    )
    p.add_argument("year", type=int, help="目标年份（如 2027）")
    p.add_argument("--solar", action="store_true", help="同时生成 solar_terms.json")
    p.add_argument("--force", action="store_true", help="覆盖已有文件")
    args = p.parse_args()

    if args.year < 2026:
        print("❌ 只支持 2026 及之后年份")
        sys.exit(1)

    generate(args.year, force=args.force, with_solar=args.solar)
