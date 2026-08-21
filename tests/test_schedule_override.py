#!/usr/bin/env python3
"""test_schedule_override.py — 写接口/override_store/plan_store/confirm 单元测试(批次 2b)"""

import json, os, re, stat, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 3, 14, 30, 0, tzinfo=CST)
TODAY = NOW.date()

from schedule.override_store import OverrideStore, OverrideError
from schedule.plan_store import PlanStore
from schedule.api import ScheduleApi, ApiRejection
from schedule.confirm import build_confirmation, build_question


def _cancel(store, when, period=3, note="临时停课"):
    return store.apply_override({"kind": "cancel", "when": when, "period": period, "note": note})


def test_apply_and_file_schema():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        r = _cancel(api, {"date": "2026-08-20"})
        assert r["ok"] is True and "text" in r
        raw = json.loads(Path(td, "schedule_overrides.json").read_text())
        assert raw["override_version"] == 1
        assert len(raw["items"]) == 1
        it = raw["items"][0]
        assert it["id"] == "ovr-20260820-1", f"id 规则 ovr-YYYYMMDD-N(条目日期), got {it['id']}"
        assert it["kind"] == "cancel" and it["date"] == "2026-08-20" and it["period"] == 3
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+0800$", it["created_at"]), \
            f"created_at 格式(CST 秒级 ISO): {it['created_at']}"
        assert stat.S_IMODE(os.stat(Path(td, "schedule_overrides.json")).st_mode) == 0o600, "0600"
    print("  OK test_apply_and_file_schema")


def test_validation_matrix():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        bads = [
            {"kind": "explode", "when": {"date": "2026-08-20"}},                       # kind 枚举
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "period": 0},           # period 越界
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "period": 12},          # period 越界
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "course": {"course": "高数"}},  # cancel 无 course
            {"kind": "move", "when": {"date": "2026-08-20"}},                          # move 必有 to_period
            {"kind": "move", "when": {"date": "2026-08-20"}, "to_period": 5},          # move 无源 period (F-A20-06)
            {"kind": "add", "when": {"date": "2026-08-20"}},                           # add 必有 course
            {"kind": "exam_week", "when": {"date": "2026-08-20"}, "period": 3},        # exam_week 无 period
            {"kind": "reminder", "when": {"date": "2026-08-20"}, "course": {"course": "x"}},  # reminder 无 course
            {"kind": "add", "when": {"date": "2026-08-20"}, "label": "x", "course": {"course": "晚自习"}},  # add/move 无 label
            {"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "交材料", "hack": 1},  # 未知字段
            {"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "交" * 60},   # label > 100 字节
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "period": 3, "note": "长" * 200},  # note > 100 字节
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "period": 3, "to_date": {"date": "2026-08-21"}},  # to_date 仅 move → 形态违规
            {"kind": "remove", "when": {"date": "2026-08-20"}},                         # apply 拒 kind=remove
        ]
        for b in bads:
            try:
                api.apply_override(b)
                raise AssertionError(f"应拒绝: {b}")
            except ApiRejection as e:
                expected = "shape_mismatch" if b.get("to_date") else "invalid_value"
                assert e.category == expected, f"{b} → {e.category}"
    print("  OK test_validation_matrix")


def test_interval_end_date_ordering():
    """R11: 区间 end_date<date 死区间 → 拒绝;==date 单日退化 / >date 正常 → 接受。
    双路径覆盖:when={"date","end_date"} 与顶层 end_date+when={"date"}。"""
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        # 路径①:when 内联区间,死区间 → 拒绝
        for item in (
            {"kind": "cancel", "period": 5, "when": {"date": "2026-08-20", "end_date": "2026-08-10"}},
            {"kind": "exam_week", "when": {"date": "2026-08-20", "end_date": "2026-08-10"}, "label": "考"},
        ):
            try:
                api.apply_override(item)
                raise AssertionError(f"死区间应拒绝: {item}")
            except ApiRejection as e:
                assert e.category == "invalid_value", f"死区间类别, got {e.category}"
        # 路径②:顶层 end_date + when 单 date,死区间 → 拒绝
        try:
            api.apply_override({"kind": "exam_week", "when": {"date": "2026-08-20"},
                                "end_date": "2026-08-10", "label": "考"})
            raise AssertionError("顶层 end_date 死区间应拒绝")
        except ApiRejection as e:
            assert e.category == "invalid_value"
        # 单日退化:end_date == date → 接受
        r = api.apply_override({"kind": "cancel", "period": 5,
                                "when": {"date": "2026-08-20", "end_date": "2026-08-20"}})
        assert r["ok"] is True
        # 正常区间:end_date > date → 接受
        r = api.apply_override({"kind": "cancel", "period": 5,
                                "when": {"date": "2026-08-20", "end_date": "2026-08-24"}})
        assert r["ok"] is True
    print("  OK test_interval_end_date_ordering")


def test_interval_span_cap_60d():
    """R11(F-A20-05):区间跨度上限 60 天(与 resolve_when {start,end} 语义一致,恰 60 允许)。
    双路径覆盖:when={date,end_date} 内联与顶层 end_date;61/78 天拒绝,修复前 {date,end_date} 绕过。"""
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=date(2026, 8, 3))
        overlong = [
            {"kind": "cancel", "period": 5, "when": {"date": "2026-08-03", "end_date": "2026-10-20"}},   # 78d
            {"kind": "cancel", "period": 5, "when": {"date": "2026-08-03", "end_date": "2026-10-03"}},   # 61d
            {"kind": "exam_week", "when": {"date": "2026-08-03", "end_date": "2026-10-20"}, "label": "考"},   # 78d
            {"kind": "cancel", "period": 5, "when": {"date": "2026-08-03"}, "end_date": "2026-10-20"},   # 顶层 end_date 78d
            {"kind": "cancel", "period": 5, "when": {"start": "2026-08-03", "end": "2026-10-20"}},       # {start,end} 对照
        ]
        for item in overlong:
            try:
                api.apply_override(item)
                raise AssertionError(f"超长区间应拒绝: {item}")
            except ApiRejection as e:
                assert e.category == "invalid_value", f"跨度类别, got {e.category}: {item}"
        # 恰 60 天允许(与 resolve_when `跨度 > 60 天` 语义一致)
        r = api.apply_override({"kind": "cancel", "period": 5,
                                "when": {"date": "2026-08-03", "end_date": "2026-10-02"}})
        assert r["ok"] is True
        # 拒绝后不得落盘超长条目
        assert all((date.fromisoformat(i["end_date"]) - date.fromisoformat(i["date"])).days <= 60
                   for i in api.overrides.items() if i.get("end_date")), "落盘条目必须满足跨度上限"
    print("  OK test_interval_span_cap_60d")


def test_end_date_normalization():
    """R11(F-A20-05):end_date 归一——MM-DD end_date 经 resolve_when 兼容解析后以 ISO 落盘
    (修复前:when 内联/顶层 MM-DD end_date 被"格式不一致拒绝")。"""
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=date(2026, 8, 3))
        # when 内联:MM-DD end_date → 归一 ISO
        r = api.apply_override({"kind": "cancel", "period": 5,
                                "when": {"date": "2026-08-20", "end_date": "08-26"}})
        assert r["ok"] is True and r["item"]["end_date"] == "2026-08-26", f"got {r['item']}"
        # 顶层:MM-DD end_date → 归一 ISO
        r = api.apply_override({"kind": "exam_week", "when": {"date": "2026-08-20"},
                                "end_date": "09-03", "label": "考"})
        assert r["ok"] is True and r["item"]["end_date"] == "2026-09-03", f"got {r['item']}"
    print("  OK test_end_date_normalization")


def test_move_requires_source_period():
    """R11(F-A20-06):move 必有源 period(无源槽的移动语义不存在)。
    修复前:无 period move 落盘 → 源槽不清(复制语义)+目标槽空课条目;修复后确定性拒绝。"""
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=date(2026, 8, 3))
        for item in (
            {"kind": "move", "when": {"date": "2026-08-04"}, "to_period": 5},          # 纯漏 period
            {"kind": "move", "when": {"date": "2026-08-04"},                           # 带 to_date 仍漏 period
             "to_date": {"date": "2026-08-21"}, "to_period": 5},
        ):
            try:
                api.apply_override(item)
                raise AssertionError(f"move 无源 period 应拒绝: {item}")
            except ApiRejection as e:
                assert e.category == "invalid_value", f"got {e.category}: {item}"
        # 空课表:带源 period 但源槽无课 → no_source_class(拒绝分支)
        try:
            api.apply_override({"kind": "move", "when": {"date": "2026-08-04"},
                                "period": 3, "to_period": 5})
            raise AssertionError("源槽无课应 no_source_class 拒绝")
        except ApiRejection as e:
            assert e.category == "no_source_class"
        assert api.overrides.items() == [], "拒绝后不得落盘"
    print("  OK test_move_requires_source_period")


def test_override_store_required_keys():
    """R11(F-A20-03):必填键校验——_load 缺 date 条目剔除并置 corrupt(防读路径 KeyError);
    add 缺 date → OverrideError(防 _next_id KeyError)。"""
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_overrides.json").write_text(json.dumps({
            "override_version": 1,
            "items": [
                {"id": "bad", "kind": "cancel", "period": 3},                      # 缺 date 必填键
                {"id": "good", "date": "2026-08-20", "kind": "cancel", "period": 2,
                 "created_at": "2026-07-01T10:00:00+08:00"}]}, ensure_ascii=False))
        store = OverrideStore(td)
        assert store.corrupt, "缺 date 必填键 → corrupt 必须置位(防 corrupt=False 永驻)"
        assert [i["id"] for i in store.items()] == ["good"], "缺 date 条目必须剔除"
        assert store.for_date(date(2026, 8, 20)) != [], "读路径不再 KeyError"
    with tempfile.TemporaryDirectory() as td2:
        store2 = OverrideStore(td2)
        try:
            store2.add({"kind": "cancel", "period": 3},
                       datetime(2026, 8, 3, 14, 30, 0, tzinfo=timezone(timedelta(hours=8))))
            raise AssertionError("缺 date 应 OverrideError")
        except OverrideError:
            pass
        except KeyError as e:   # 修复前:_next_id(entry["date"]) KeyError
            raise AssertionError(f"缺 date 必填键不得抛 KeyError: {e}")
    print("  OK test_override_store_required_keys")


def test_apply_override_rejection_branches():
    """R11 拒绝分支覆盖补全(盲区 0 覆盖):past_date/before_semester/after_semester 直测。"""
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=date(2026, 8, 3))
        try:
            api.apply_override({"kind": "cancel", "when": {"date": "2026-08-01"}, "period": 3})
            raise AssertionError("过去日期应拒绝")
        except ApiRejection as e:
            assert e.category == "past_date"
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=date(2026, 1, 5))
        try:
            api.apply_override({"kind": "add", "when": {"week_offset": 1}, "period": 3,
                                "course": {"course": "晚自习"}})
            raise AssertionError("学期前 week_offset 应拒绝")
        except ApiRejection as e:
            assert e.category == "before_semester"
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23",
                                            "semester_end": "2026-07-04"}},
                          today=date(2026, 6, 29))
        try:
            api.apply_override({"kind": "add", "when": {"week_offset": 1}, "period": 3,
                                "course": {"course": "晚自习"}})
            raise AssertionError("目标周超出学期应拒绝")
        except ApiRejection as e:
            assert e.category == "after_semester"
    print("  OK test_apply_override_rejection_branches")


def test_idempotent_write():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        api.apply_override({"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "交材料"})
        api.apply_override({"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "交材料"})
        assert len(api.overrides.items()) == 1, "reminder date+label 全等 → 替换不新增"
        api.apply_override({"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "交别的"})
        assert len(api.overrides.items()) == 2, "label 不同 → 新增"
        api.apply_override({"kind": "exam_week", "when": {"date": "2026-08-20"}, "end_date": "2026-08-26", "label": "期末考试周"})
        api.apply_override({"kind": "exam_week", "when": {"date": "2026-08-20"}, "end_date": "2026-08-26", "label": "期末考试周"})
        assert len([i for i in api.overrides.items() if i["kind"] == "exam_week"]) == 1, "exam_week 全等 → 替换"
    print("  OK test_idempotent_write")


def test_remove_override():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        _cancel(api, {"date": "2026-08-20"}, period=3)
        _cancel(api, {"date": "2026-08-20", "end_date": "2026-08-24"}, period=5)
        # 按 id 删
        it = api.overrides.items()[0]
        r = api.remove_override({"id": it["id"]})
        assert r["ok"] is True
        # 区间性 cancel 按起始日 date+period 删整条(不做全等)
        r = api.remove_override({"date": "2026-08-20", "period": 5})
        assert r["ok"] is True and api.overrides.items() == []
        # 零匹配 → not_found
        try:
            api.remove_override({"date": "2026-09-01", "period": 1})
            raise AssertionError("零匹配应拒绝")
        except ApiRejection as e:
            assert e.category == "not_found"
    print("  OK test_remove_override")


def test_cleanup_endpoints():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_overrides.json").write_text(json.dumps({
            "override_version": 1,
            "items": [
                {"id": "c1", "date": "2026-08-01", "kind": "cancel", "period": 1,
                 "created_at": "2026-07-01T10:00:00+08:00"},
                {"id": "c2", "date": "2026-07-20", "end_date": "2026-07-24", "kind": "cancel",
                 "period": 3, "created_at": "2026-07-01T10:00:00+08:00"},
                {"id": "m2", "date": "2026-07-01", "to_date": "2026-07-02", "kind": "move",
                 "period": 1, "to_period": 7,
                 "course": {"course": "线代", "teacher": "", "weeks": [1], "weeks_raw": "第1周",
                            "location": "", "alternates": []},
                 "created_at": "2026-07-01T10:00:00+08:00"},
                {"id": "e1", "date": "2026-06-01", "end_date": "2026-06-05", "kind": "exam_week",
                 "label": "期末", "created_at": "2026-06-01T10:00:00+08:00"},
                {"id": "r1", "date": "2026-07-30", "kind": "reminder", "label": "已过提醒",
                 "created_at": "2026-07-01T10:00:00+08:00"}]}, ensure_ascii=False))
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        api.apply_override({"kind": "cancel", "when": {"date": "2027-08-20"},
                            "end_date": "2027-08-24", "period": 2})   # 区间 end 未到 → 留
        api.apply_override({"kind": "add", "when": {"date": "2027-08-03"}, "period": 8,
                            "course": {"course": "高数"}})          # 先造源槽(add 例外,F-A20-06 后 move 必带源 period)
        api.apply_override({"kind": "move", "when": {"date": "2027-08-03"},
                            "period": 8, "to_date": {"date": "2027-08-14"}, "to_period": 7})
        api.apply_override({"kind": "reminder", "when": {"date": "2027-08-20"}, "label": "未来提醒"})
        api.apply_override({"kind": "add", "when": {"date": "2027-08-20"}, "period": 9,
                            "course": {"course": "晚自习"}})
        api.overrides.cleanup(TODAY)
        kinds = [(i["kind"], i["date"]) for i in api.overrides.items()]
        assert ("cancel", "2027-08-20") in kinds, "区间性 cancel end 未到必须保留"
        assert ("move", "2027-08-03") in kinds, "跨天 move 目标日未到必须保留(按 max(date,to_date))"
        assert ("move", "2026-07-01") not in kinds, "跨天 move 双端已过必须清"
        assert ("exam_week", "2026-06-01") not in kinds, "exam_week end 已过必须清"
        assert ("reminder", "2026-07-30") not in kinds and ("reminder", "2027-08-20") in kinds
        assert all(i["kind"] != "cancel" or i["date"] != "2026-08-01" for i in api.overrides.items())
        assert all(i["kind"] != "cancel" or i["date"] != "2026-07-20" for i in api.overrides.items())
    print("  OK test_cleanup_endpoints")


def test_corrupt_and_migration_order():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_overrides.json").write_text("{broken")
        Path(td, "anniversaries.json").write_text("{also broken")
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        # 读路径先验证:损坏 → 空集 + 不落盘
        assert api.overrides.items() == []
        assert api.anniversary_mgr.visible_items(), "anniversaries 损坏 → 视同缺失合并默认"
        api._guard()  # 首次写类调用点触发迁移
        # 子项 0:overrides 重建为合法空文件(非 0 字节)
        raw = json.loads(Path(td, "schedule_overrides.json").read_text())
        assert raw == {"override_version": 1, "items": []}, f"重建必须为合法空文件, got {raw}"
        # 子项 ①:anniversaries 重建为默认生日(0 先于 ①)
        anns = json.loads(Path(td, "anniversaries.json").read_text())
        assert any(a["name"] == "迟菓生日" for a in anns["anniversaries"])
        # ② 已激活(6c):历史 countdown → 迁移为 reminder(豁免过去校验)并从文件移除
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": [
            {"id": "c1", "type": "countdown", "name": "考试", "date": "2026-12-25", "note": "", "created_at": "2026-08-01"}]}))
        api2 = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        api2._guard()
        rem = [i for i in api2.overrides.items() if i["kind"] == "reminder"]
        assert len(rem) == 1 and rem[0]["date"] == "2026-12-25" and rem[0]["label"] == "考试", \
            f"② 激活,countdown 迁为 reminder, got {rem}"
        assert not any(a["type"] == "countdown"
                       for a in json.loads(Path(td, "anniversaries.json").read_text())["anniversaries"]), \
            "已迁移条目从 anniversaries.json 移除"
    print("  OK test_corrupt_and_migration_order")


def test_toml_exam_weeks_migration():
    """③:toml exam_weeks 一次性迁移(判据 kind==exam_week 且 date+end_date 全等,幂等 M9);
    单日期条目 → date=end_date 单日退化(F14);键删除后不再迁移"""
    import copy
    with tempfile.TemporaryDirectory() as td:
        cfg = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04",
                            "special_dates": [], "exam_weeks": ["2026-06-22,2026-07-03"]}}
        api = ScheduleApi(td, cfg, today=TODAY)   # 批 4 激活 = __init__ 默认 True(Step 3)
        api._guard()
        ew = [i for i in api.overrides.items() if i["kind"] == "exam_week"]
        assert len(ew) == 1 and ew[0]["date"] == "2026-06-22" and ew[0]["end_date"] == "2026-07-03"
        assert ew[0]["label"] == "from toml 考试周", f"label 合成规则, got {ew[0]['label']}"
        # 幂等:再次迁移不新增
        api._migrated = False
        api._guard()
        assert len([i for i in api.overrides.items() if i["kind"] == "exam_week"]) == 1
        # 单日期条目 → 单日退化
        with tempfile.TemporaryDirectory() as td2:
            cfg2 = dict(cfg)
            cfg2["schedule"] = dict(cfg["schedule"], exam_weeks=["2026-06-22"])
            api2 = ScheduleApi(td2, cfg2, today=TODAY)
            api2._guard()
            ew2 = [i for i in api2.overrides.items() if i["kind"] == "exam_week"]
            assert ew2[0]["date"] == ew2[0]["end_date"] == "2026-06-22", f"单日退化, got {ew2[0]}"
    print("  OK test_toml_exam_weeks_migration")


def test_toml_special_dates_migration():
    """④:special_dates 合并(迟菓生日默认跳过,其余 name='特殊日期 MM-DD' note='from toml');
    文件缺失时创建含默认生日文件(R1,防'④先建文件不含默认→合并失效→默认永久缺席')"""
    with tempfile.TemporaryDirectory() as td:
        cfg = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04",
                            "special_dates": ["05-11", "11-03"], "exam_weeks": []}}
        api = ScheduleApi(td, cfg, today=TODAY)
        api._guard()
        anns = json.loads(Path(td, "anniversaries.json").read_text())["anniversaries"]
        names = {a["name"] for a in anns}
        assert "迟菓生日" in names, "④ 缺失时创建含默认生日文件(R1)"
        assert "特殊日期 11-03" in names, f"name 合成规则 M1, got {names}"
        it = next(a for a in anns if a["name"] == "特殊日期 11-03")
        assert it["date"] == "11-03" and it.get("note") == "from toml", f"got {it}"
        # 幂等:再次迁移不重复
        api._migrated = False
        api._guard()
        anns2 = json.loads(Path(td, "anniversaries.json").read_text())["anniversaries"]
        assert len([a for a in anns2 if a["name"] == "特殊日期 11-03"]) == 1
    print("  OK test_toml_special_dates_migration")


def test_plan_store_and_confirm():
    with tempfile.TemporaryDirectory() as td:
        ps = PlanStore(td)
        assert ps.load() is None and ps.generated_at() is None, "缺失 → None"
        ps.save({"plan_version": 1, "generated_at": "2026-08-03T15:00:00+08:00", "modifiers": []})
        assert ps.load()["plan_version"] == 1 and ps.generated_at() == "2026-08-03T15:00:00+08:00"
        assert stat.S_IMODE(os.stat(Path(td, "schedule_plan.json")).st_mode) == 0o600, "0600"
        Path(td, "schedule_plan.json").write_text("{broken")
        assert ps.load() is None, "损坏 → None(恒等 1.0 语义)"
        # confirm 模板:含星期数+日期(L1)
        t = build_confirmation({"kind": "reminder", "date": "2026-08-20", "label": "交材料"})
        assert "8月20日" in t and "周四" in t, f"确认文案须含星期+日期, got {t}"
        t2 = build_confirmation({"kind": "exam_week", "date": "2026-08-20", "end_date": "2026-08-26", "label": "期末考试周"})
        assert "考试周" in t2 and "期末考试周" in t2
        q, missing = build_question("past_date")
        assert "过去了" in q and "date" in missing
        q2, m2 = build_question("no_source_class")
        assert "没有课" in q2 and "period" in m2
    print("  OK test_plan_store_and_confirm")


def test_corrupt_mixed_items_preserves_valid_on_rebuild():
    """回归(issue #308):混合坏/好条目文件 → 迁移重建不得清空好条目。
    1 坏条目(period 越界) + ≥2 合法条目(reminder/cancel) → 触发写路径:
    好条目保留落盘、坏条目剔除、文件有 .bak 备份。"""
    import shutil
    with tempfile.TemporaryDirectory() as td:
        good_rem = {"id": "r1", "date": "2026-08-20", "kind": "reminder",
                    "label": "交材料", "created_at": "2026-07-01T10:00:00+08:00"}
        good_cancel = {"id": "c1", "date": "2026-08-21", "kind": "cancel",
                       "period": 2, "note": "临时停课",
                       "created_at": "2026-07-01T10:00:00+08:00"}
        bad = {"id": "b1", "date": "2026-08-22", "kind": "cancel", "period": 12,  # period 越界 -> _load 剔除
               "created_at": "2026-07-01T10:00:00+08:00"}
        Path(td, "schedule_overrides.json").write_text(
            json.dumps({"override_version": 1, "items": [good_rem, bad, good_cancel]},
                       ensure_ascii=False))
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        # 读路径:坏条目剔除、好条目留在内存
        assert api.overrides.corrupt, "含坏条目 → corrupt=True"
        assert [i["id"] for i in api.overrides.items()] == ["r1", "c1"], \
            f"_load 应保留好条目剔除坏条目, got {[i['id'] for i in api.overrides.items()]}"
        api._guard()  # 触发迁移写路径
        raw = json.loads(Path(td, "schedule_overrides.json").read_text())
        kept = raw["items"]
        ids = [i["id"] for i in kept]
        assert "r1" in ids and "c1" in ids, f"好条目必须保留落盘, got {ids}"
        assert "b1" not in ids, f"坏条目必须剔除, got {ids}"
        assert len(kept) == 2, f"恰好 1 坏剔除 + 2 好保留, got {len(kept)}"
        assert Path(td, "schedule_overrides.json.bak").exists(), \
            "重建写前必须有 .bak 备份"
        bak = json.loads(Path(td, "schedule_overrides.json.bak").read_text())
        assert len(bak["items"]) == 3, f".bak 必须含原始坏/好混合内容, got {len(bak['items'])}"
        # 二次实例化:重建后文件干净、corrupt 复位、好条目仍可读
        api2 = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        assert not api2.overrides.corrupt, "重建后 corrupt 必须复位"
        assert "r1" in [i["id"] for i in api2.overrides.items()]
    print("  OK test_corrupt_mixed_items_preserves_valid_on_rebuild")


def test_corrupt_fullfile_still_rebuilds_empty():
    """对照(不回归):整文件 JSON 损坏 → 仍重建为空集(现有行为保持)。
    分隔:损坏解析失败场景与"坏/好混合"场景语义不同,不得依赖修复合一。"""
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_overrides.json").write_text("{broken")
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        assert api.overrides.items() == []
        api._guard()
        raw = json.loads(Path(td, "schedule_overrides.json").read_text())
        assert raw == {"override_version": 1, "items": []}, f"整文件损坏须重建为空集, got {raw}"
        api2 = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}}, today=TODAY)
        assert not api2.overrides.corrupt
    print("  OK test_corrupt_fullfile_still_rebuilds_empty")
