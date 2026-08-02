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


if __name__ == "__main__":
    print("test_schedule_parser.py\n")
    tests = [
        test_parse_weeks_single, test_parse_weeks_range, test_parse_weeks_odd_even,
        test_parse_weeks_comma, test_parse_weeks_garbage,
        test_parse_cell_single_hyphen, test_parse_cell_single_space,
        test_parse_cell_merged_two_courses, test_parse_cell_trailing_location_fragment,
        test_parse_cell_truncated_course, test_parse_cell_empty,
    ]
    for t in tests:
        t()
    print(f"\n{'='*40}")
    print(f"ALL {len(tests)} tests passed.")
