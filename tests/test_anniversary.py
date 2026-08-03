#!/usr/bin/env python3
"""test_anniversary.py — schedule/anniversary 单元测试(迁移 + 默认初始化 + mmdd_to_date)"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import tempfile
from datetime import date
from pathlib import Path
from schedule.anniversary import AnniversaryManager, mmdd_to_date, DEFAULT_ANNIVERSARIES


def test_crud_and_list():
    with tempfile.TemporaryDirectory() as td:
        mgr = AnniversaryManager(td)
        a = mgr.add("anniversary", "哥哥的生日", "03-01")
        assert a.type == "anniversary" and a.date == "03-01"
        got = mgr.list_all()
        assert len(got) == 1 and got[0].name == "哥哥的生日"
        assert mgr.remove(a.id) is True and mgr.remove(a.id) is False
        b = mgr.add("anniversary", "认识纪念日", "07-08", note="x")
        up = mgr.update(b.id, name="认识N周年")
        assert up is not None and up.name == "认识N周年"
        # 落盘校验
        raw = json.loads(Path(td, "anniversaries.json").read_text())
        assert raw["anniversaries"][0]["name"] == "认识N周年"
    print("  OK test_crud_and_list")


def test_default_merge_missing_or_corrupt():
    with tempfile.TemporaryDirectory() as td:
        # 文件缺失 → 合并默认(不落盘)
        mgr = AnniversaryManager(td)
        names = {a["name"] for a in mgr.visible_items()}
        assert "迟菓生日" in names, "缺失时默认生日必须可见"
        assert not Path(td, "anniversaries.json").exists(), "读路径不得落盘"
        # 文件损坏 → 视同缺失合并默认
        Path(td, "anniversaries.json").write_text("{broken")
        mgr2 = AnniversaryManager(td)
        names2 = {a["name"] for a in mgr2.visible_items()}
        assert "迟菓生日" in names2, "损坏时默认生日必须可见"
        # 文件存在 → 不合并(用户已写文件,默认不覆盖)
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": [
            {"id": "x1", "type": "anniversary", "name": "用户条目", "date": "01-01", "note": "", "created_at": "2026-01-01"}]}))
        mgr3 = AnniversaryManager(td)
        names3 = {a["name"] for a in mgr3.visible_items()}
        assert names3 == {"用户条目"}, f"文件存在不得合并默认, got {names3}"
        # 用户删除默认后不写回:写入非默认条目,文件仍无迟菓生日
        a = mgr3.add("anniversary", "另一个", "02-02")
        assert a.name == "另一个"
        raw = json.loads(Path(td, "anniversaries.json").read_text())
        assert all(x["name"] != "迟菓生日" for x in raw["anniversaries"])
    print("  OK test_default_merge_missing_or_corrupt")


def test_mmdd_to_date():
    assert mmdd_to_date("02-29", 2026) == date(2026, 2, 28), "非闰年兜底"
    assert mmdd_to_date("02-29", 2028) == date(2028, 2, 29), "闰年保留"
    assert mmdd_to_date("08-20", 2026) == date(2026, 8, 20)
    print("  OK test_mmdd_to_date")


def test_get_today_upcoming_kept():
    with tempfile.TemporaryDirectory() as td:
        mgr = AnniversaryManager(td)
        mgr.add("anniversary", "生日", "08-20")
        today = date(2026, 8, 20)
        assert [a.name for a in mgr.get_today(today)] == ["生日"]
        got = mgr.get_upcoming(date(2026, 8, 1), days=30)
        assert any(a.name == "生日" and delta == 19 for a, delta in got)
    print("  OK test_get_today_upcoming_kept")


def test_special_dates_merge_kept_behavior():
    """④ 合并后:文件存在不覆盖用户删除;默认生日仍可见(已在文件中)"""
    with tempfile.TemporaryDirectory() as td:
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": [
            {"id": "u1", "type": "anniversary", "name": "用户条目", "date": "01-01",
             "note": "", "created_at": "2026-01-01"}]}))
        from schedule.api import ScheduleApi
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04",
                                            "special_dates": ["05-11", "11-03"], "exam_weeks": []}},
                          today=date(2026, 8, 5))
        api._guard()
        names = {a.name for a in api.anniversary_mgr.list_all()}
        assert "用户条目" in names, "用户文件不丢"
        assert not any(a.name == "迟菓生日" for a in api.anniversary_mgr.list_all()), \
            "文件存在不合并默认(用户曾删默认)"
    print("  OK test_special_dates_merge_kept_behavior")


if __name__ == "__main__":
    print("test_anniversary.py\n")
    tests = [test_crud_and_list, test_default_merge_missing_or_corrupt,
             test_mmdd_to_date, test_get_today_upcoming_kept,
             test_special_dates_merge_kept_behavior]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
