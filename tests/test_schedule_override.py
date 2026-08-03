#!/usr/bin/env python3
"""test_schedule_override.py — 写接口/override_store/plan_store/confirm 单元测试(批次 2b)"""

import json, os, stat, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
NOW = datetime(2026, 8, 3, 14, 30, 0, tzinfo=CST)
TODAY = NOW.date()

from schedule.override_store import OverrideStore
from schedule.plan_store import PlanStore
from schedule.api import ScheduleApi, ApiRejection
from schedule.confirm import build_confirmation, build_question


def _cancel(store, when, period=3, note="临时停课"):
    return store.apply_override({"kind": "cancel", "when": when, "period": period, "note": note})


def test_apply_and_file_schema():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
        r = _cancel(api, {"date": "2026-08-20"})
        assert r["ok"] is True and "text" in r
        raw = json.loads(Path(td, "schedule_overrides.json").read_text())
        assert raw["override_version"] == 1
        assert len(raw["items"]) == 1
        it = raw["items"][0]
        assert it["id"] == "ovr-20260820-1", f"id 规则 ovr-YYYYMMDD-N(条目日期), got {it['id']}"
        assert it["kind"] == "cancel" and it["date"] == "2026-08-20" and it["period"] == 3
        assert it["created_at"].startswith("2026-08-03T") and it["created_at"].endswith("+0800"), \
            f"created_at CST 秒级, got {it['created_at']}"
        assert stat.S_IMODE(os.stat(Path(td, "schedule_overrides.json")).st_mode) == 0o600, "0600"
    print("  OK test_apply_and_file_schema")


def test_validation_matrix():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
        bads = [
            {"kind": "explode", "when": {"date": "2026-08-20"}},                       # kind 枚举
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "period": 0},           # period 越界
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "period": 12},          # period 越界
            {"kind": "cancel", "when": {"date": "2026-08-20"}, "course": {"course": "高数"}},  # cancel 无 course
            {"kind": "move", "when": {"date": "2026-08-20"}},                          # move 必有 to_period
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


def test_idempotent_write():
    with tempfile.TemporaryDirectory() as td:
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
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
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
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
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
        api.apply_override({"kind": "cancel", "when": {"date": "2026-08-20"},
                            "end_date": "2026-08-24", "period": 2})   # 区间 end 未到 → 留
        api.apply_override({"kind": "move", "when": {"date": "2026-08-03"},
                            "to_date": {"date": "2026-08-14"}, "to_period": 7,
                            "course": {"course": "高数"}})
        api.apply_override({"kind": "reminder", "when": {"date": "2026-08-20"}, "label": "未来提醒"})
        api.apply_override({"kind": "add", "when": {"date": "2026-08-20"}, "period": 9,
                            "course": {"course": "晚自习"}})
        api.overrides.cleanup(TODAY)
        kinds = [(i["kind"], i["date"]) for i in api.overrides.items()]
        assert ("cancel", "2026-08-20") in kinds, "区间性 cancel end 未到必须保留"
        assert ("move", "2026-08-03") in kinds, "跨天 move 目标日未到必须保留(按 max(date,to_date))"
        assert ("move", "2026-07-01") not in kinds, "跨天 move 双端已过必须清"
        assert ("exam_week", "2026-06-01") not in kinds, "exam_week end 已过必须清"
        assert ("reminder", "2026-07-30") not in kinds and ("reminder", "2026-08-20") in kinds
        assert all(i["kind"] != "cancel" or i["date"] != "2026-08-01" for i in api.overrides.items())
        assert all(i["kind"] != "cancel" or i["date"] != "2026-07-20" for i in api.overrides.items())
    print("  OK test_cleanup_endpoints")


def test_corrupt_and_migration_order():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_overrides.json").write_text("{broken")
        Path(td, "anniversaries.json").write_text("{also broken")
        api = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
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
        api2 = ScheduleApi(td, {"schedule": {"semester_start": "2026-02-23"}})
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


if __name__ == "__main__":
    print("test_schedule_override.py\n")
    tests = [test_apply_and_file_schema, test_validation_matrix, test_idempotent_write,
             test_remove_override, test_cleanup_endpoints, test_corrupt_and_migration_order,
             test_toml_exam_weeks_migration, test_toml_special_dates_migration,
             test_plan_store_and_confirm]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
