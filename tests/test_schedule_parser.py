#!/usr/bin/env python3
"""test_schedule_parser.py — schedule 模块单元测试（解析/查询/缓存）"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from schedule_parser import ScheduleParser

CST = timezone(timedelta(hours=8))

def dt(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=CST)

# ── 解析层:周数 ──

def test_parse_weeks_single():
    assert ScheduleParser._parse_weeks("19周") == {19}
    print("  OK test_parse_weeks_single")

def test_parse_weeks_range():
    assert ScheduleParser._parse_weeks("2-17周") == set(range(2, 18))
    print("  OK test_parse_weeks_range")

def test_parse_weeks_odd_even():
    assert ScheduleParser._parse_weeks("10-16(双)周") == {10, 12, 14, 16}
    assert ScheduleParser._parse_weeks("3-15单") == {3, 5, 7, 9, 11, 13, 15}
    print("  OK test_parse_weeks_odd_even")

def test_parse_weeks_comma():
    assert ScheduleParser._parse_weeks("2-4,6,8-10周") == {2, 3, 4, 6, 8, 9, 10}
    print("  OK test_parse_weeks_comma")

def test_parse_weeks_garbage():
    assert ScheduleParser._parse_weeks("abc") == set()
    print("  OK test_parse_weeks_garbage")

# ── 解析层:单元格 ──

def test_parse_cell_single_hyphen():
    r = ScheduleParser._parse_cell("高等数学BII(理论)-刘洋【2-17周】尚行楼")
    assert len(r) == 1 and r[0]["course"] == "高等数学BII(理论)"
    assert r[0]["teacher"] == "刘洋"
    assert r[0]["weeks"] == set(range(2, 18))
    assert r[0]["location"] == "尚行楼"
    print("  OK test_parse_cell_single_hyphen")

def test_parse_cell_single_space():
    r = ScheduleParser._parse_cell("管理学基础(理论) 王芳【2-17周】 南楼610智慧教室")
    assert len(r) == 1 and r[0]["course"] == "管理学基础(理论)"
    assert r[0]["teacher"] == "王芳"
    print("  OK test_parse_cell_single_space")

def test_parse_cell_merged_two_courses():
    cell = "工程CAD实训-张伟【19周】尚行楼304 BIM实训室4  管理学基础(理论) 王芳【2-17周】 南楼610智慧教室"
    r = ScheduleParser._parse_cell(cell)
    assert len(r) == 2, r
    assert r[0]["course"] == "工程CAD实训" and r[1]["course"] == "管理学基础(理论)"
    print("  OK test_parse_cell_merged_two_courses")

def test_parse_cell_trailing_location_fragment():
    # 尾部纯 location 残片（location 内含 2+ 空白）→ 回退整 cell 解析
    r = ScheduleParser._parse_cell("工程CAD实训-张伟【19周】尚行楼304  BIM实训室4")
    assert len(r) == 1 and r[0]["course"] == "工程CAD实训", r
    print("  OK test_parse_cell_trailing_location_fragment")

def test_parse_cell_truncated_course():
    # 失败段含【 → 残缺课程丢弃
    r = ScheduleParser._parse_cell("高等数学-刘洋【2-17周】尚行楼  残缺课")
    assert len(r) == 1 and r[0]["course"] == "高等数学", r
    print("  OK test_parse_cell_truncated_course")

def test_parse_cell_empty():
    assert ScheduleParser._parse_cell("") == []
    assert ScheduleParser._parse_cell("  ") == []
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
    assert ScheduleParser._current_period(dt(2026, 6, 15, 8, 0)) == 1
    assert ScheduleParser._current_period(dt(2026, 6, 15, 8, 47)) is None  # 课间
    assert ScheduleParser._current_period(dt(2026, 6, 15, 20, 40)) == 11
    assert ScheduleParser._current_period(dt(2026, 6, 15, 22, 0)) is None  # 晚课后
    print("  OK test_current_period_boundaries")


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
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
