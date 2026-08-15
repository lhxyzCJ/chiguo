#!/usr/bin/env python3
"""test_holiday_parser.py — holiday_parser 单元测试"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from schedule.holiday import HolidayParser  # import 迁移（原 from holiday_parser import ...）
import json
import tempfile
from pathlib import Path


def test_known_holiday():
    hp = HolidayParser()
    # 2026 法定节假日
    assert hp.is_holiday(date(2026, 10, 1)), "国庆节 should be holiday"
    assert hp.holiday_name(date(2026, 10, 1)) == "国庆节"
    assert hp.is_holiday(date(2026, 2, 15)), "春节 should be holiday"
    assert hp.holiday_name(date(2026, 2, 15)) == "春节"
    assert hp.is_holiday(date(2026, 1, 1)), "元旦 should be holiday"
    assert hp.is_holiday(date(2026, 5, 1)), "劳动节 should be holiday"
    assert hp.is_holiday(date(2026, 6, 20)), "端午节 should be holiday"
    print("  OK test_known_holiday")

def test_holiday_range():
    hp = HolidayParser()
    # 假期中间日期也应识别
    assert hp.is_holiday(date(2026, 10, 3)), "Oct 3 within 国庆 range"
    assert hp.is_holiday(date(2026, 2, 18)), "Feb 18 within 春节 range"
    # 假期前一天/后一天
    assert not hp.is_holiday(date(2026, 9, 30)), "Sep 30 not holiday"
    assert not hp.is_holiday(date(2026, 10, 8)), "Oct 8 not holiday"
    print("  OK test_holiday_range")

def test_non_holiday():
    hp = HolidayParser()
    assert not hp.is_holiday(date(2026, 3, 15))
    assert not hp.is_holiday(date(2026, 8, 1))
    assert hp.holiday_name(date(2026, 3, 15)) is None
    print("  OK test_non_holiday")

def test_makeup_workday():
    hp = HolidayParser()
    # 2026-01-04 是周日但元旦调休上班
    assert hp.is_makeup_workday(date(2026, 1, 4))
    assert not hp.is_weekend(date(2026, 1, 4))  # 调休日不算周末
    assert hp.is_school_day(date(2026, 1, 4))   # 调休日要上学
    print("  OK test_makeup_workday")

def test_weekend():
    hp = HolidayParser()
    # 普通周六
    assert hp.is_weekend(date(2026, 3, 7))   # Saturday
    assert hp.is_weekend(date(2026, 3, 8))   # Sunday
    assert not hp.is_weekend(date(2026, 3, 9))  # Monday
    # 节假日中的周末也算周末
    assert hp.is_weekend(date(2026, 10, 3))  # Saturday in 国庆
    print("  OK test_weekend")

def test_school_day():
    hp = HolidayParser()
    assert hp.is_school_day(date(2026, 3, 4))    # 周三，非节假日
    assert not hp.is_school_day(date(2026, 10, 1))  # 国庆节
    assert not hp.is_school_day(date(2026, 3, 7))   # 周六
    assert hp.is_school_day(date(2026, 1, 4))    # 周日调休上班
    print("  OK test_school_day")

def test_query():
    hp = HolidayParser()
    q = hp.query(date(2026, 10, 1))
    assert q["is_holiday"] == True
    assert q["holiday_name"] == "国庆节"
    assert "hint" in q
    assert "国庆" in q["hint"]
    print("  OK test_query")


def test_range_of_and_all_ranges():
    hp = HolidayParser()
    r = hp.range_of("国庆节")
    assert r == (date(2026, 10, 1), date(2026, 10, 7)), f"got {r}"
    assert hp.range_of("不存在节日") is None
    ar = hp.all_ranges()
    assert isinstance(ar, dict) and ar["国庆节"] == (date(2026, 10, 1), date(2026, 10, 7))
    ar2 = hp.all_ranges()
    assert ar2 is not ar, "all_ranges 必须返回副本"
    ar2["国庆节"] = (date(2026, 1, 1), date(2026, 1, 1))
    assert hp.range_of("国庆节") == (date(2026, 10, 1), date(2026, 10, 7)), "外部修改不得影响内部"
    print("  OK test_range_of_and_all_ranges")


def test_override_merge_and_corrupt():
    with tempfile.TemporaryDirectory() as td:
        good = Path(td) / "holidays.json"
        good.write_text(json.dumps({"holidays": {"自定义假": {"start": "2026-08-20", "end": "2026-08-22"}}}))
        hp = HolidayParser(str(good))
        assert hp.range_of("自定义假") == (date(2026, 8, 20), date(2026, 8, 22)), "json override 应合并"
        assert hp.range_of("国庆节") == (date(2026, 10, 1), date(2026, 10, 7)), "内嵌不得丢失"
        bad = Path(td) / "holidays_bad.json"
        bad.write_text("{not json")
        hp2 = HolidayParser(str(bad))
        assert hp2.range_of("国庆节") == (date(2026, 10, 1), date(2026, 10, 7)), "损坏 override → 跳过仅用内嵌"
    print("  OK test_override_merge_and_corrupt")



