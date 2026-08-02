#!/usr/bin/env python3
"""test_schedule_parser.py — schedule 模块单元测试（解析/查询/缓存）"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from schedule import ScheduleParser
from schedule.parsing import parse_cell, parse_weeks
from schedule.query import current_period

CST = timezone(timedelta(hours=8))

def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CST)

# ── 解析层:周数 ──

def test_parse_weeks_single():
    assert parse_weeks("19周") == {19}
    print("  OK test_parse_weeks_single")

def test_parse_weeks_range():
    assert parse_weeks("2-17周") == set(range(2, 18))
    print("  OK test_parse_weeks_range")

def test_parse_weeks_odd_even():
    assert parse_weeks("10-16(双)周") == {10, 12, 14, 16}
    assert parse_weeks("3-15单") == {3, 5, 7, 9, 11, 13, 15}
    print("  OK test_parse_weeks_odd_even")

def test_parse_weeks_comma():
    assert parse_weeks("2-4,6,8-10周") == {2, 3, 4, 6, 8, 9, 10}
    print("  OK test_parse_weeks_comma")

def test_parse_weeks_garbage():
    assert parse_weeks("abc") == set()
    print("  OK test_parse_weeks_garbage")

# ── 解析层:单元格 ──

def test_parse_cell_single_hyphen():
    r = parse_cell("高等数学BII(理论)-刘洋【2-17周】尚行楼")
    assert len(r) == 1 and r[0]["course"] == "高等数学BII(理论)"
    assert r[0]["teacher"] == "刘洋"
    assert r[0]["weeks"] == set(range(2, 18))
    assert r[0]["location"] == "尚行楼"
    print("  OK test_parse_cell_single_hyphen")

def test_parse_cell_single_space():
    r = parse_cell("管理学基础(理论) 王芳【2-17周】 南楼610智慧教室")
    assert len(r) == 1 and r[0]["course"] == "管理学基础(理论)"
    assert r[0]["teacher"] == "王芳"
    print("  OK test_parse_cell_single_space")

def test_parse_cell_merged_two_courses():
    cell = "工程CAD实训-张伟【19周】尚行楼304 BIM实训室4  管理学基础(理论) 王芳【2-17周】 南楼610智慧教室"
    r = parse_cell(cell)
    assert len(r) == 2, r
    assert r[0]["course"] == "工程CAD实训" and r[1]["course"] == "管理学基础(理论)"
    print("  OK test_parse_cell_merged_two_courses")

def test_parse_cell_trailing_location_fragment():
    # 尾部纯 location 残片（location 内含 2+ 空白）→ 回退整 cell 解析
    r = parse_cell("工程CAD实训-张伟【19周】尚行楼304  BIM实训室4")
    assert len(r) == 1 and r[0]["course"] == "工程CAD实训", r
    print("  OK test_parse_cell_trailing_location_fragment")

def test_parse_cell_truncated_course():
    # 失败段含【 → 残缺课程丢弃
    r = parse_cell("高等数学-刘洋【2-17周】尚行楼  残缺课")
    assert len(r) == 1 and r[0]["course"] == "高等数学", r
    print("  OK test_parse_cell_truncated_course")

def test_parse_cell_empty():
    assert parse_cell("") == []
    assert parse_cell("  ") == []
    print("  OK test_parse_cell_empty")

# ── 查询层（手构 schedule,不经 xlsx）──

def _mk(weeks, course="高数", teacher="刘洋", loc="尚行楼"):
    return {"course": course, "teacher": teacher,
            "weeks": set(weeks), "weeks_raw": "", "location": loc}

def _parser_with(schedule, semester_start=date(2026, 2, 23)):
    p = ScheduleParser.__new__(ScheduleParser)
    p.enabled = True
    p.available = True
    p._schedule = schedule
    p._parsed_at = 0
    p.xlsx_path = None
    p.cache_path = None
    p.semester_start = semester_start
    p._ensure_parsed = lambda: None  # 查询层单测:跳过文件刷新
    return p

def test_query_in_class():
    p = _parser_with({0: {3: _mk([2, 17])}})
    r = p.query(dt(2026, 6, 15, 10, 30))  # 周一第3节（10:00-10:45）,学期第17周
    assert r["in_class"] is True
    assert r["current_course"]["course"] == "高数"
    assert r["current_course"]["period"] == 3
    assert r["current_course"]["minutes_remaining"] == 15
    print("  OK test_query_in_class")

def test_query_not_in_class_break():
    p = _parser_with({0: {3: _mk([2, 17])}})
    r = p.query(dt(2026, 6, 15, 10, 47))  # 课间
    assert r["in_class"] is False
    print("  OK test_query_not_in_class_break")

def test_query_next_course():
    p = _parser_with({0: {3: _mk([2, 17]), 4: _mk([2, 17], course="英语")}})
    r = p.query(dt(2026, 6, 15, 9, 0))
    assert r["in_class"] is False
    assert r["next_course"]["course"] == "高数"
    assert r["periods_today"][0]["period"] == 3
    print("  OK test_query_next_course")

def test_query_alternates_week_filter():
    # 合并单元格:2-17 周主课 vs 19 周备选
    entry = {**_mk([2, 17]), "alternates": [_mk([19], course="实训")]}
    p = _parser_with({0: {3: entry}})
    r = p.query(dt(2026, 6, 29, 10, 30))  # 2026-06-29 = 第19周（Jul 6 是第20周!）
    assert r["current_course"]["course"] == "实训", r["current_course"]
    r2 = p.query(dt(2026, 6, 15, 10, 30))  # 第17周 → 主课
    assert r2["current_course"]["course"] == "高数"
    print("  OK test_query_alternates_week_filter")

def test_query_class_load():
    p = _parser_with({0: {1: _mk([2, 17]), 2: _mk([2, 17]), 3: _mk([2, 17]),
                          4: _mk([2, 17]), 5: _mk([2, 17]), 6: _mk([2, 17])}})
    assert p.query(dt(2026, 6, 15, 8, 0))["class_load"] == "heavy"
    p2 = _parser_with({0: {1: _mk([2, 17])}})
    assert p2.query(dt(2026, 6, 15, 8, 0))["class_load"] == "light"
    p3 = _parser_with({})
    r = p3.query(dt(2026, 6, 15, 8, 0))
    assert r["class_load"] == "free" and r["in_class"] is False
    print("  OK test_query_class_load")

def test_query_week_boundary():
    p = _parser_with({0: {1: _mk([17])}})
    assert p.query(dt(2026, 6, 15, 8, 0))["in_class"] is True   # 第17周
    assert p.query(dt(2026, 6, 22, 8, 0))["in_class"] is False  # 第18周,无课
    print("  OK test_query_week_boundary")

def test_current_period_boundaries():
    assert current_period(dt(2026, 6, 15, 8, 0)) == 1
    assert current_period(dt(2026, 6, 15, 8, 47)) is None  # 课间
    assert current_period(dt(2026, 6, 15, 20, 40)) == 11
    assert current_period(dt(2026, 6, 15, 22, 0)) is None  # 晚课后
    print("  OK test_current_period_boundaries")

# ── xlsx 解析 + 缓存（真实 openpyxl fixture）──

import tempfile, json, time
from pathlib import Path
import openpyxl

def _write_xlsx(path: Path, rows: list):
    wb = openpyxl.Workbook()
    ws = wb.active
    for i, row in enumerate(rows, start=5):  # 第5行起是课表数据
        ws.cell(row=i, column=1, value=row[0])
        for j, cell in enumerate(row[1:], start=2):
            ws.cell(row=i, column=j, value=cell)
    wb.save(str(path))

ROWS = [
    [1, "高等数学BII(理论)-刘洋【2-17周】尚行楼", None, None, None, None, None, None],
    [3, None, None, "管理学基础(理论) 王芳【2-17周】 南楼610智慧教室", None, None, None, None],
    [4, "工程CAD实训-张伟【19周】尚行楼304 BIM实训室4  管理学基础(理论) 王芳【2-17周】 南楼610智慧教室", None, None, None, None, None, None],
]

def test_xlsx_parse_and_query():
    with tempfile.TemporaryDirectory() as td:
        xp = Path(td) / "xskb.xlsx"
        _write_xlsx(xp, ROWS)
        p = ScheduleParser(xlsx_path=str(xp), cache_path=str(Path(td) / "c.json"),
                           semester_start=date(2026, 2, 23))
        assert p.available is True
        # 周一第1节,第17周（col 2 = weekday 0）
        r = p.query(dt(2026, 6, 15, 8, 30))
        assert r["in_class"] is True and r["current_course"]["course"] == "高等数学BII(理论)"
        # 周三第3节（col 3 = weekday 2）→ 空格分隔格式
        r2 = p.query(dt(2026, 6, 17, 10, 30))
        assert r2["current_course"]["course"] == "管理学基础(理论)", r2
        # 周一第4节合并单元格,主课=工程CAD(仅19周):第17周 → 备选管理学;第19周 → 主课
        r3 = p.query(dt(2026, 6, 15, 11, 0))
        assert r3["current_course"]["course"] == "管理学基础(理论)", r3
        r4 = p.query(dt(2026, 6, 29, 11, 0))
        assert r4["current_course"]["course"] == "工程CAD实训", r4
        print("  OK test_xlsx_parse_and_query")

def test_cache_roundtrip_and_reparse_on_mtime():
    with tempfile.TemporaryDirectory() as td:
        xp = Path(td) / "xskb.xlsx"
        _write_xlsx(xp, ROWS)
        cp = Path(td) / "c.json"
        p1 = ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        assert cp.exists()  # 缓存已落盘
        # 新实例直接读缓存（不重新解析）
        p2 = ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        assert p2.available and p2.query(dt(2026, 6, 15, 8, 30))["in_class"]
        # 修改 xlsx → mtime 变化 → 重新解析
        time.sleep(1.1)
        _write_xlsx(xp, [[1, "新课程-老师【2-17周】新地点", None, None, None, None, None, None]])
        p3 = ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        r = p3.query(dt(2026, 6, 15, 8, 30))
        assert r["current_course"]["course"] == "新课程", r
        print("  OK test_cache_roundtrip_and_reparse_on_mtime")

def test_cache_corrupt_is_ignored():
    with tempfile.TemporaryDirectory() as td:
        xp = Path(td) / "xskb.xlsx"
        _write_xlsx(xp, ROWS)
        cp = Path(td) / "c.json"
        cp.write_text("{corrupt json")
        p = ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        assert p.available is True  # 坏缓存被删 + 重新解析 xlsx
        # 坏缓存被替换为有效缓存（xlsx 存在 → 重解析后重新落盘）
        assert json.loads(cp.read_text())["cache_version"] == 2, cp.read_text()
        print("  OK test_cache_corrupt_is_ignored")

def test_parse_failure_keeps_old_cache():
    with tempfile.TemporaryDirectory() as td:
        xp = Path(td) / "xskb.xlsx"
        _write_xlsx(xp, ROWS)
        cp = Path(td) / "c.json"
        ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        xp.write_text("not an xlsx")  # 损坏 → 解析失败
        time.sleep(1.1)
        os.utime(xp)  # 确保 mtime 变化
        p = ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        r = p.query(dt(2026, 6, 15, 8, 30))
        # 实际语义:_parse 失败置 _schedule={}（内存空课表）→ in_class=False;
        # available 由缓存加载置 True 且失败解析不重置（历史行为,特征锁定）;
        # 缓存文件保留（不用空数据覆盖落盘）
        assert r["in_class"] is False and r["available"] is True, r
        assert cp.exists(), "失败解析不应覆盖旧缓存文件"
        print("  OK test_parse_failure_keeps_old_cache")

def test_cache_v1_migration_forces_reparse():
    with tempfile.TemporaryDirectory() as td:
        xp = Path(td) / "xskb.xlsx"
        _write_xlsx(xp, ROWS)
        cp = Path(td) / "c.json"
        cp.write_text(json.dumps({"cache_version": 1, "parsed_at": 0, "schedule": {}}))
        p = ScheduleParser(xlsx_path=str(xp), cache_path=str(cp), semester_start=date(2026, 2, 23))
        assert p.query(dt(2026, 6, 15, 8, 30))["in_class"] is True  # v1 缓存强制重解析
        print("  OK test_cache_v1_migration_forces_reparse")


if __name__ == "__main__":
    print("test_schedule_parser.py\n")
    tests = [
        test_parse_weeks_single, test_parse_weeks_range, test_parse_weeks_odd_even,
        test_parse_weeks_comma, test_parse_weeks_garbage,
        test_parse_cell_single_hyphen, test_parse_cell_single_space,
        test_parse_cell_merged_two_courses, test_parse_cell_trailing_location_fragment,
        test_parse_cell_truncated_course, test_parse_cell_empty,
        test_query_in_class, test_query_not_in_class_break, test_query_next_course,
        test_query_alternates_week_filter, test_query_class_load,
        test_query_week_boundary, test_current_period_boundaries,
        test_xlsx_parse_and_query, test_cache_roundtrip_and_reparse_on_mtime,
        test_cache_corrupt_is_ignored, test_parse_failure_keeps_old_cache,
        test_cache_v1_migration_forces_reparse,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
