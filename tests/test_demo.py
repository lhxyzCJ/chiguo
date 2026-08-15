#!/usr/bin/env python3
"""test_demo.py — 演示模式隔离 + 节假日估算单元测试

覆盖:
- Bug1(update_holidays.py): 农历偏移方向 —— 2028 春节必须早于 2027 模板(2027-02-06)
- Bug2(update_holidays.py): chinese_calendar dict 分支的键名可读性与区间保留
- Bug4(chiguo_demo.py): 演示模式退出时 state.save() 不得写回生产 chiguo_state.json

注: 新增测试文件白名单仅此一个,故 update_holidays 的测试也收在这里。
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import types
from collections import namedtuple
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update_holidays as uh

PROJ = Path(__file__).resolve().parent.parent
PROD_STATE = PROJ / "chiguo_state.json"


# ═══════════════════════════════════════════════════════════
# Bug1: 农历偏移方向(update_holidays.py)
# ═══════════════════════════════════════════════════════════

def test_2028_spring_earlier_than_2027():
    """Bug1: 农历偏移方向。2028 春节(月日)必须早于 2027 模板春节(2027-02-06)。"""
    from datetime import date as _date
    h2027 = uh.get_holidays_for(2027)
    h2028 = uh.get_holidays_for(2028)
    assert h2027["春节"][0] == "2027-02-06", h2027["春节"]
    # 跨年比较须按「月日」而非完整日期字符串(字符串序 2028-01-26 > 2027-02-06,语义相反)
    d2027 = _date.fromisoformat(h2027["春节"][0])
    d2028 = _date.fromisoformat(h2028["春节"][0])
    assert (d2028.month, d2028.day) < (d2027.month, d2027.day), \
        f"2028 春节({h2028['春节'][0]}) 应早于 2027 春节({h2027['春节'][0]}): 农历年比公历年短,春节逐年提前"
    print("  OK test_2028_spring_earlier_than_2027")


def test_lunar_offset_exact_2028():
    """Bug1 精确值:2028 春节 = 2027-02-06 − 11 天 = 2028-01-26(估算分支)。"""
    orig = uh.try_chinese_calendar
    uh.try_chinese_calendar = lambda year: None  # 强制走估算分支,不依赖外部包
    try:
        h = uh.get_holidays_for(2028)
        assert h["春节"][0] == "2028-01-26", h["春节"]
        assert h["端午节"][0] == "2028-05-29", h["端午节"]  # 2027-06-09 − 11d
        assert h["中秋节"][0] == "2028-09-04", h["中秋节"]  # 2027-09-15 − 11d
        # 固定日期假期不受偏移影响
        assert h["元旦"][0] == "2028-01-01"
        assert h["劳动节"][0] == "2028-05-01"
        assert h["国庆节"][0] == "2028-10-01"
    finally:
        uh.try_chinese_calendar = orig
    print("  OK test_lunar_offset_exact_2028")


def test_lunar_offset_2026_direction():
    """Bug1 反向:2026 春节应晚于 2027(往过去偏移 +11 天)。"""
    from datetime import date as _date
    orig = uh.try_chinese_calendar
    uh.try_chinese_calendar = lambda year: None
    try:
        h = uh.get_holidays_for(2026)
        spring = h["春节"][0]
        d = _date.fromisoformat(spring)
        # 跨年比较按「月日」:2026-02-15 晚于 2027-02-06(字符串序相反,须按月日)
        assert (d.month, d.day) > (2, 6), f"2026 春节({spring}) 应晚于 2027 春节"
    finally:
        uh.try_chinese_calendar = orig
    print("  OK test_lunar_offset_2026_direction")


# ═══════════════════════════════════════════════════════════
# Bug2: chinese_calendar dict 分支(update_holidays.py)
# ═══════════════════════════════════════════════════════════

def _with_fake_cc(holidays_map):
    """临时注入 fake chinese_calendar 模块,返回 try_chinese_calendar 结果。"""
    fake = types.ModuleType("chinese_calendar")
    fake.get_holidays = lambda year: holidays_map
    old = sys.modules.get("chinese_calendar")
    sys.modules["chinese_calendar"] = fake
    try:
        return uh.try_chinese_calendar(2028)
    finally:
        if old is None:
            sys.modules.pop("chinese_calendar", None)
        else:
            sys.modules["chinese_calendar"] = old


def test_cc_dict_branch_namedtuple():
    """Bug2: dict 分支(value 为 Holiday namedtuple)→ 输出可读名字,不丢日期。"""
    Holiday = namedtuple("Holiday", "name date")
    r = _with_fake_cc({
        date(2028, 1, 26): Holiday(name="春节", date=date(2028, 1, 26)),
        date(2028, 10, 1): Holiday(name="国庆节", date=date(2028, 10, 1)),
    })
    assert r is not None, "dict 分支不应返回 None"
    assert set(r) == {"春节", "国庆节"}, f"键名应为可读中文名: {r}"
    assert r["春节"] == ("2028-01-26", "2028-01-26"), r["春节"]
    assert r["国庆节"] == ("2028-10-01", "2028-10-01"), r["国庆节"]
    print("  OK test_cc_dict_branch_namedtuple")


def test_cc_dict_branch_date_value():
    """Bug2: dict 分支(value 本身是 date,无 name)→ 键回退用 key。"""
    r = _with_fake_cc({date(2028, 1, 26): date(2028, 1, 26)})
    assert r == {"2028-01-26": ("2028-01-26", "2028-01-26")}, f"应回退 key 为键: {r}"
    print("  OK test_cc_dict_branch_date_value")


def test_cc_dict_branch_str_value():
    """Bug2: dict 分支(value 为名字字符串,{date: "春节"})→ 键取 value 字符串。"""
    r = _with_fake_cc({date(2028, 1, 26): "春节"})
    assert r == {"春节": ("2028-01-26", "2028-01-26")}, f"字符串 value 应作键名: {r}"
    print("  OK test_cc_dict_branch_str_value")


def test_cc_dict_branch_range_kept():
    """Bug2: dict 分支(value 带 start_date/end_date)→ 优先保留区间信息。"""
    HolidayRange = namedtuple("Holiday", "name start_date end_date")
    r = _with_fake_cc({
        date(2028, 1, 1): HolidayRange(name="元旦",
                                       start_date=date(2028, 1, 1),
                                       end_date=date(2028, 1, 3)),
    })
    assert r["元旦"] == ("2028-01-01", "2028-01-03"), f"区间应保留: {r}"
    print("  OK test_cc_dict_branch_range_kept")


# ═══════════════════════════════════════════════════════════
# Bug4: 演示模式状态隔离(chiguo_demo.py)
# ═══════════════════════════════════════════════════════════

def _sha(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None


def _make_demo(td: str):
    """注入演示目录环境变量后构造 Demo(import chiguo_demo 有 chdir 副作用,已固定到项目根)。"""
    with mock.patch.dict(os.environ, {"CHIGUO_DEMO_BASE_DIR": td}):
        import chiguo_demo
        return chiguo_demo.Demo()


def test_cc_dict_branch_multi_day_same_name_aggregated():
    """R4: 真实 chinese_calendar 对多日假期每天一条同名条目 → 按名聚合 min/max，不塌缩为最后一天。"""
    Holiday = namedtuple("Holiday", "name date")  # 真实包结构：无 start_date/end_date
    r = _with_fake_cc({
        date(2028, 1, 29): Holiday(name="春节", date=date(2028, 1, 29)),
        date(2028, 1, 30): Holiday(name="春节", date=date(2028, 1, 30)),
        date(2028, 1, 31): Holiday(name="春节", date=date(2028, 1, 31)),
        date(2028, 2, 1): Holiday(name="春节", date=date(2028, 2, 1)),
    })
    assert r["春节"] == ("2028-01-29", "2028-02-01"), f"多日假期应聚合为区间: {r}"
    print("  OK test_cc_dict_branch_multi_day_same_name_aggregated")


def test_demo_state_path_isolated():
    """演示模式 state 文件必须指向独立目录,绝不指向生产 chiguo_state.json。"""
    with tempfile.TemporaryDirectory() as td:
        demo = _make_demo(td)
        sp = demo.state.state_path
        assert sp != PROD_STATE, f"演示模式不得使用生产状态文件: {sp}"
        assert str(sp).startswith(td), f"演示状态应落在独立目录: {sp}"
        assert sp.name == "chiguo_state.json"
    print("  OK test_demo_state_path_isolated")


def test_demo_run_keeps_production_state_untouched():
    """演示模式完整跑一轮(空行推进 + q 退出)后,生产 chiguo_state.json 内容不变。"""
    before = _sha(PROD_STATE)
    with tempfile.TemporaryDirectory() as td:
        demo = _make_demo(td)
        with mock.patch("sys.stdin", io.StringIO("\nq\n")):
            demo.run()
        demo_state = Path(td) / "chiguo_state.json"
        assert demo_state.exists(), "演示状态应写入独立目录下的 chiguo_state.json"
    after = _sha(PROD_STATE)
    assert after == before, "生产 chiguo_state.json 被演示模式修改!"
    print("  OK test_demo_run_keeps_production_state_untouched")


def test_holidays_file_covers_year():
    """R22: _file_covers_year 识别文件已含某年数据(_generated_for 匹配或任一 start 落该年)"""
    assert uh._file_covers_year(
        {"holidays": {"元旦": {"start": "2026-01-01", "end": "2026-01-03"}}}, 2026)
    assert not uh._file_covers_year(
        {"holidays": {"元旦": {"start": "2026-01-01", "end": "2026-01-03"}}}, 2027)
    assert uh._file_covers_year(
        {"holidays": {"元旦@2027": {"start": "2027-01-01", "end": "2027-01-03"}}}, 2027)
    assert uh._file_covers_year({"_generated_for": "2027"}, 2027)
    assert not uh._file_covers_year(None, 2026)   # 非 dict → False
    assert not uh._file_covers_year([], 2026)     # 列表 → False
    print("  OK test_holidays_file_covers_year")


def test_holidays_merge_semantics():
    """R22: _merge_holidays 同名同年→覆盖;同名不同年→归组 name@year 追加;新名→追加"""
    existing = {"元旦": {"start": "2026-01-01", "end": "2026-01-03"}}
    new = {
        "元旦": {"start": "2027-01-01", "end": "2027-01-03"},
        "春节": {"start": "2027-02-06", "end": "2027-02-14"},
    }
    merged = uh._merge_holidays(existing, 2027, new)
    assert merged["元旦"]["start"] == "2026-01-01"              # 旧年保留
    assert merged["元旦@2027"]["start"] == "2027-01-01"         # 新年归组追加
    assert merged["春节"]["start"] == "2027-02-06"              # 新名追加

    # 同名同年 → 覆盖更新,不追加 @year
    m2 = uh._merge_holidays({"元旦": {"start": "2026-01-01"}}, 2026,
                            {"元旦": {"start": "2026-01-02"}})
    assert m2["元旦"]["start"] == "2026-01-02"
    assert "元旦@2026" not in m2
    print("  OK test_holidays_merge_semantics")


def test_holidays_generate_cross_year_merge():
    """R22: generate() 跨年自动合并(旧年保留/新年追加),同年重复生成拒绝覆盖"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        with mock.patch.object(uh, "BASE_DIR", td):
            # 2026 精确生成
            uh.generate(2026)
            p = td / "holidays.json"
            assert p.exists()
            data = json.loads(p.read_text(encoding="utf-8"))
            assert "元旦" in data["holidays"]

            # 2027 估算 → 跨年合并:元旦@2027 追加,2026 元旦保留
            uh.generate(2027)
            data = json.loads(p.read_text(encoding="utf-8"))
            assert data["holidays"]["元旦"]["start"].startswith("2026")
            assert "元旦@2027" in data["holidays"]
            assert data["holidays"]["元旦@2027"]["start"].startswith("2027")
            assert data["_generated_for"] == "2027"

            # 2027 再次生成 → 拒绝覆盖,文件不变
            before = p.read_text(encoding="utf-8")
            uh.generate(2027)
            assert p.read_text(encoding="utf-8") == before, "同年重复生成应拒绝覆盖"

            # 2026 再次生成 → 已含 2026,拒绝
            before = p.read_text(encoding="utf-8")
            uh.generate(2026)
            assert p.read_text(encoding="utf-8") == before, "已含 2026 数据应拒绝覆盖"
    print("  OK test_holidays_generate_cross_year_merge")
