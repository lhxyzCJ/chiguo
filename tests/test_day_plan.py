#!/usr/bin/env python3
"""test_day_plan.py — 检索层/窄原语/resolve_when/api 校验单元测试(批次 2c)"""

import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
SS = date(2026, 2, 23)          # 周一,学期第 1 周(周界锚定)
TODAY = date(2026, 8, 5)        # 周三;学期第 24 周(08-03 周一 ~ 08-07 周五)
NOW = datetime(2026, 8, 5, 14, 0, tzinfo=CST)   # 周三 14:00 = 第 5 节上课中

from schedule.sources import load_sources
from schedule.day_plan import (week_number, week_courses, resolve_classes,
                               availability_base, class_load_adjust)
from schedule.resolve_when import resolve_when, ResolveReject
from schedule.attention import t1_items, t2_block, t3_window, today_exceptions, build_attention
from schedule.recall import recall
from schedule.api import ScheduleApi, ApiRejection


def _cfg(**sched_overrides):
    sched = {"semester_start": "2026-02-23", "semester_end": "2026-07-04",
             "special_dates": ["05-11", "11-03"], "exam_weeks": []}
    sched.update(sched_overrides)
    return {"schedule": sched}


def _write_overrides(td, items):
    Path(td, "schedule_overrides.json").write_text(
        json.dumps({"override_version": 1, "items": items}, ensure_ascii=False))


def _write_anniversaries(td, items):
    Path(td, "anniversaries.json").write_text(
        json.dumps({"anniversaries": items}, ensure_ascii=False))


def _mk(td, cfg=None, cache=None, breaks=None, anniv=None, ovr=None):
    cfg = cfg if cfg is not None else _cfg()
    if breaks is not None:
        Path(td, "break_state.json").write_text(json.dumps(breaks, ensure_ascii=False))
    if anniv is not None:
        _write_anniversaries(td, anniv)
    if ovr is not None:
        _write_overrides(td, ovr)
    return load_sources(td, cfg, schedule_cache_dict=cache)


# ═══ week_number / week_courses(§4.1)═══

def test_week_number_monday_alignment():
    """非周一开学:周界对齐周一(十九轮 F7)"""
    ss = date(2026, 8, 5)  # 周三开学
    assert week_number(date(2026, 8, 3), ss) == 1, "开学周前的周一仍属第 1 周"
    assert week_number(date(2026, 8, 5), ss) == 1
    assert week_number(date(2026, 8, 10), ss) == 2, "次周周一应进第 2 周"
    # semester_start 恰为周一 → 与原式逐值一致
    assert week_number(date(2026, 3, 2), SS) == 2
    print("  OK test_week_number_monday_alignment")


def test_week_courses_active_and_alternates():
    """active 过滤 + alternates 周次互斥 + 学期末不钳制"""
    cache = {"schedule": {"0": {"3": {"course": "高数", "teacher": "刘洋", "weeks": [2, 3, 4],
                                     "weeks_raw": "2-4周", "location": "A301", "alternates": [
                                         {"course": "高数A", "teacher": "刘洋", "weeks": [19],
                                          "weeks_raw": "19周", "location": "B202", "alternates": []}]}}}}
    sched = load_sources("x", _cfg(), schedule_cache_dict=cache).schedule
    assert 3 in week_courses(sched, SS, 3)[0], "weeks 含该周 → active"
    c19 = week_courses(sched, SS, 19)[0][3]
    assert c19["course"] == "高数A", "alternates 周次互斥 → 19 周命中备选"
    assert 3 not in week_courses(sched, SS, 5)[0], "无课周 → 空"
    assert 3 not in week_courses(sched, SS, 30)[0], "学期末不钳制,无课匹配自然为空"
    print("  OK test_week_courses_active_and_alternates")


# ═══ day_plan 事实窗口(§4)═══

def test_day_plan_no_files_on_read():
    """零写断言:读路径不产生任何文件(M5)"""
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td)
        availability_base(NOW, src)
        t1_items(src, TODAY)
        recall("考试周", src, TODAY)
        resolve_when({"date": "2026-08-20"}, TODAY, SS)
        assert list(Path(td).iterdir()) == [], f"读路径零写, got {list(Path(td).iterdir())}"
    print("  OK test_day_plan_no_files_on_read")

# ═══ resolve_classes / 窄原语(§5.1)═══

CACHE_3PERIOD = {"schedule": {"2": {"3": {"course": "高数", "teacher": "刘洋", "weeks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
                                         "weeks_raw": "2-17周", "location": "A301", "alternates": []},
                                    "5": {"course": "线代", "teacher": "王芳", "weeks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
                                          "weeks_raw": "2-17周", "location": "B202", "alternates": []}},
                 "4": {"3": {"course": "英语", "teacher": "赵敏", "weeks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
                             "weeks_raw": "2-17周", "location": "C303", "alternates": []}}}}


def test_resolve_classes_move_dual_slot():
    """move 双槽位语义 ①-④(M10/HIGH-1)"""
    D = "2026-08-05"  # 周三(week 24 → cache weeks 无 24 → 基底空) → 用第 2 周周三
    D = "2026-03-04"  # 周三,第 2 周
    base = CACHE_3PERIOD
    # ① move 目标槽与基底冲突 → move 课替换呈现,源槽空置
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, cache=base, ovr=[
            {"id": "m1", "date": D, "kind": "move", "period": 3, "to_period": 5,
             "course": {"course": "高数", "teacher": "刘洋", "weeks": [2], "weeks_raw": "第2周",
                        "location": "A301", "alternates": []},
             "created_at": "2026-03-01T10:00:00+08:00"}])
        rc = resolve_classes(date.fromisoformat(D), src)
        assert 3 not in rc, "源槽空置"
        assert rc[5]["action"] == "move" and rc[5]["course"] == "高数", "目标槽替换呈现"
    # ② 后写 cancel 命中目标槽 → 移除 move 课,恢复基底(uncancelled)
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, cache=base, ovr=[
            {"id": "m1", "date": D, "kind": "move", "period": 3, "to_period": 5,
             "course": {"course": "高数", "teacher": "刘洋", "weeks": [2], "weeks_raw": "第2周",
                        "location": "A301", "alternates": []},
             "created_at": "2026-03-01T10:00:00+08:00"},
            {"id": "c1", "date": D, "kind": "cancel", "period": 5,
             "created_at": "2026-03-02T10:00:00+08:00"}])
        rc = resolve_classes(date.fromisoformat(D), src)
        assert rc[5]["course"] == "线代" and rc[5].get("cancelled") is not True, "② 恢复基底不取消"
        assert rc[5]["source"] == "schedule"
    # ③ 后写 cancel 命中源槽 → no-op(源槽已空,无对象可作用)
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, cache=base, ovr=[
            {"id": "m1", "date": D, "kind": "move", "period": 3, "to_period": 5,
             "course": {"course": "高数", "teacher": "刘洋", "weeks": [2], "weeks_raw": "第2周",
                        "location": "A301", "alternates": []},
             "created_at": "2026-03-01T10:00:00+08:00"},
            {"id": "c2", "date": D, "kind": "cancel", "period": 3,
             "created_at": "2026-03-02T10:00:00+08:00"}])
        rc = resolve_classes(date.fromisoformat(D), src)
        assert 3 not in rc and rc[5]["action"] == "move", "③ 源槽命中 no-op,move 本体仍在"
    # 跨天 move:源日空置,目标日呈现
    D2 = "2026-03-05"  # 周四
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, cache=base, ovr=[
            {"id": "m2", "date": D, "kind": "move", "period": 3, "to_period": 3, "to_date": D2,
             "course": {"course": "高数", "teacher": "刘洋", "weeks": [2], "weeks_raw": "第2周",
                        "location": "A301", "alternates": []},
             "created_at": "2026-03-01T10:00:00+08:00"}])
        rcsrc = resolve_classes(date.fromisoformat(D), src)
        assert 3 not in rcsrc, "源日空置"
        rcdst = resolve_classes(date.fromisoformat(D2), src)
        assert rcdst[3]["action"] == "move" and rcdst[3]["course"] == "高数", "目标日呈现 move"
    # 区间性 cancel 按日展开
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, cache=base, ovr=[
            {"id": "i1", "date": D, "end_date": "2026-03-06", "kind": "cancel", "period": 5,
             "note": "本周停课", "created_at": "2026-03-01T10:00:00+08:00"},
            {"id": "i2", "date": D, "end_date": "2026-03-06", "kind": "cancel", "period": 3,
             "note": "本周停课", "created_at": "2026-03-01T10:00:00+08:00"}])
        rc2 = resolve_classes(date(2026, 3, 4), src)
        assert rc2[5]["cancelled"] is True, "区间性 cancel 每日展开"
        rc3 = resolve_classes(date(2026, 3, 6), src)
        assert rc3[3]["cancelled"] is True, "区间性 cancel 中间日(03-06 周五)同样展开"
    print("  OK test_resolve_classes_move_dual_slot")


def test_availability_tiers_and_overlap():
    """五档 tier + C5 重叠优先级"""
    def ab(d, ovr=None, breaks=None, cfg=None, cache=None):
        with tempfile.TemporaryDirectory() as td:
            return availability_base(datetime(d.year, d.month, d.day, 12, 0, tzinfo=CST),
                                     _mk(td, cfg=cfg, cache=cache, breaks=breaks, ovr=ovr))
    # idle_school(课表可用)
    r = ab(date(2026, 3, 4), cache=CACHE_3PERIOD)
    assert r == {"base": 0.85, "tier": "idle_school"}, f"got {r}"
    # unavailable(课表不可用)
    r = ab(date(2026, 3, 4))
    assert r == {"base": 1.0, "tier": "unavailable"}, f"got {r}"
    # weekend → non_school(需 semester_end 在 8/8 之后,否则进 break)
    r = ab(date(2026, 8, 8), cfg=_cfg(semester_end="2027-01-01"))  # 周六
    assert r == {"base": 0.85, "tier": "non_school"}
    # 考周×周末 → exam 0.5(与现码一致:in_exam 判定先于周末)
    r = ab(date(2026, 8, 8), cfg=_cfg(semester_end="2027-01-01"), ovr=[
        {"id": "e1", "date": "2026-08-03", "end_date": "2026-08-09", "kind": "exam_week",
         "label": "期末", "created_at": "2026-08-01T10:00:00+08:00"}])
    assert r == {"base": 0.5, "tier": "exam"}, f"考周×周末 → 0.5, got {r}"
    # 考周×法定节假日 → 节假日 0.85(嵌套 elif,holiday 先判)
    r = ab(date(2026, 10, 1), cfg=_cfg(semester_end="2027-01-01"), ovr=[
        {"id": "e2", "date": "2026-09-28", "end_date": "2026-10-02", "kind": "exam_week",
         "label": "期末", "created_at": "2026-09-01T10:00:00+08:00"}])
    assert r == {"base": 0.85, "tier": "non_school"}, f"考周×节假日 → 0.85, got {r}"
    # 考周×寒暑假 → break 0.85
    r = ab(date(2026, 8, 8), cfg=_cfg(semester_end="2027-01-01"), ovr=[
        {"id": "e3", "date": "2026-08-03", "end_date": "2026-08-09", "kind": "exam_week",
         "label": "期末", "created_at": "2026-08-01T10:00:00+08:00"}],
        breaks={"breaks": [{"start": "2026-07-01", "end": "2026-08-31", "note": "暑假"}]})
    assert r == {"base": 0.85, "tier": "break"}, f"考周×寒暑假 → 0.85, got {r}"
    # 学期结束 → break
    r = ab(date(2026, 8, 8))
    assert r == {"base": 0.85, "tier": "break"}, f"semester_end 已过 → break, got {r}"
    print("  OK test_availability_tiers_and_overlap")


def test_class_load_adjust():
    """第二层:上课中档位 + 课间剩余节数桶序(0→0.85,1→0.70,其余→0.50);cancelled 不计入剩余"""
    def classes_for(*periods):
        return {p: {"period": p, "course": "x", "source": "schedule", "cancelled": False}
                for p in periods}
    def cancelled(rc, p):
        rc[p]["cancelled"] = True
        return rc
    # 上课中:heavy/normal/light
    assert class_load_adjust(0.85, classes_for(1, 2, 3, 4, 5, 6), NOW) == 0.05
    assert class_load_adjust(0.85, classes_for(5, 6, 7), NOW) == 0.08
    assert class_load_adjust(0.85, classes_for(5), NOW) == 0.12
    # 课间(09:36,current_period None):剩余节数桶序
    between = datetime(2026, 8, 5, 9, 36, tzinfo=CST)
    assert class_load_adjust(0.85, classes_for(1, 2), between) == 0.85, "剩余 0 → 0.85"
    assert class_load_adjust(0.85, classes_for(5, 6), between) == 0.50, "剩余 2 → 0.50"
    assert class_load_adjust(0.85, classes_for(5, 6, 7), between) == 0.50, "剩余 ≥2 → 0.50"
    # cancel → remaining 减少 → availability 上升(例外生效由此体现)
    rc = classes_for(5, 6, 7, 8, 9)
    assert class_load_adjust(0.85, cancelled(rc, 6), between) == 0.50
    rc2 = classes_for(5, 6)
    assert class_load_adjust(0.85, cancelled(rc2, 6), between) == 0.70, "cancel 后剩余 1 → 0.70"
    rc3 = classes_for(5)
    assert class_load_adjust(0.85, cancelled(rc3, 5), between) == 0.85, "cancel 后剩余 0 → 0.85"
    print("  OK test_class_load_adjust")


def _reject(when, **kw):
    try:
        resolve_when(when, kw.get("today", TODAY), kw.get("ss", SS))
    except ResolveReject:
        return
    raise AssertionError(f"应拒绝: {when!r}")


def test_rw_explicit_and_mmdd():
    assert resolve_when({"date": "2026-08-20"}, TODAY, SS) == (date(2026, 8, 20), date(2026, 8, 20))
    assert resolve_when({"date": "08-20"}, TODAY, SS) == (date(2026, 8, 20), date(2026, 8, 20)), "今年未过 → 留今年"
    assert resolve_when({"date": "08-03"}, TODAY, SS) == (date(2027, 8, 3), date(2027, 8, 3)), "今年已过 → 明年(inferYear)"
    assert resolve_when({"date": "02-29"}, TODAY, SS) == (date(2027, 2, 28), date(2027, 2, 28)), "02-29 非闰年兜底 02-28"
    assert resolve_when({"date": "08-05"}, TODAY, SS) == (date(2026, 8, 5), date(2026, 8, 5)), "当天(未过)留今年"
    print("  OK test_rw_explicit_and_mmdd")


def test_rw_days():
    assert resolve_when({"days": 2}, TODAY, SS) == (date(2026, 8, 7), date(2026, 8, 7))
    _reject({"days": 0}); _reject({"days": -1})
    _reject({"days": 2.5}); _reject({"days": "2"})
    print("  OK test_rw_days")


def test_rw_weekday():
    """目标星期数 > 今天 → 本周;否则(含当天)→ 下周同星期(F1/MED-4)"""
    assert resolve_when({"weekday": 4}, TODAY, SS) == (date(2026, 8, 6), date(2026, 8, 6)), "周四 > 周三 → 本周四"
    assert resolve_when({"weekday": 3}, TODAY, SS) == (date(2026, 8, 12), date(2026, 8, 12)), "当天 → 下周三"
    assert resolve_when({"weekday": 2}, TODAY, SS) == (date(2026, 8, 11), date(2026, 8, 11)), "周二 < 周三 → 下周二"
    assert resolve_when({"weekday": 7}, TODAY, SS) == (date(2026, 8, 9), date(2026, 8, 9)), "周日 → 本周日"
    _reject({"weekday": 0}); _reject({"weekday": 8}); _reject({"weekday": "3"})
    print("  OK test_rw_weekday")


def test_rw_week_offset_interval():
    """week_offset 单形态 = 区间,结束日 = 周五(M11);仅 0|1"""
    assert resolve_when({"week_offset": 0}, TODAY, SS) == (date(2026, 8, 3), date(2026, 8, 7)), "本周 周一~周五"
    assert resolve_when({"week_offset": 1}, TODAY, SS) == (date(2026, 8, 10), date(2026, 8, 14)), "下周 周一~周五"
    _reject({"week_offset": 2}); _reject({"week_offset": -1}); _reject({"week_offset": 0.5})
    print("  OK test_rw_week_offset_interval")


def test_rw_combo():
    """组合 {week_offset, weekday} = 单日,与单 weekday 已过→下周规则无关(F-C)"""
    assert resolve_when({"week_offset": 1, "weekday": 3}, TODAY, SS) == (date(2026, 8, 12), date(2026, 8, 12)), "下周三"
    assert resolve_when({"week_offset": 0, "weekday": 3}, TODAY, SS) == (date(2026, 8, 5), date(2026, 8, 5)), "{0,当天}=今天"
    assert resolve_when({"week_offset": 0, "weekday": 2}, TODAY, SS) == (date(2026, 8, 4), date(2026, 8, 4)), "{0,已过}=本周二(api 层拒过去)"
    assert resolve_when({"week_offset": 0, "weekday": 5}, TODAY, SS) == (date(2026, 8, 7), date(2026, 8, 7)), "{0,未来}=本周五"
    _reject({"week_offset": 1, "weekday": 8}); _reject({"week_offset": 2, "weekday": 3})
    _reject({"week_offset": 1, "weekday": "3"})
    print("  OK test_rw_combo")


def test_rw_start_end():
    """C1 算法:转写 → 跨年滚 end → 终校验(跨度 ≤60 恰允/倒序拒/单日退化/混合年裁定)"""
    assert resolve_when({"start": "2026-08-20", "end": "2026-08-22"}, TODAY, SS) == \
        (date(2026, 8, 20), date(2026, 8, 22)), "显式双端"
    assert resolve_when({"start": "08-20", "end": "08-22"}, TODAY, SS) == \
        (date(2026, 8, 20), date(2026, 8, 22)), "双 MM-DD 当年"
    assert resolve_when({"start": "12-28", "end": "01-03"}, TODAY, SS) == \
        (date(2026, 12, 28), date(2027, 1, 3)), "end 月份 < start 月份 → end 加一年"
    _reject({"start": "01-15", "end": "01-03"}), "同月倒序拒绝"
    assert resolve_when({"start": "2026-06-01", "end": "2026-07-31"}, TODAY, SS) == \
        (date(2026, 6, 1), date(2026, 7, 31)), "恰 60 天 = 允许(边界钉死)"
    _reject({"start": "2026-06-01", "end": "2026-08-01"}), "61 天拒绝"
    assert resolve_when({"start": "2026-08-20", "end": "2026-08-20"}, TODAY, SS) == \
        (date(2026, 8, 20), date(2026, 8, 20)), "start==end 单日退化"
    assert resolve_when({"start": "2026-12-28", "end": "01-03"}, TODAY, SS) == \
        (date(2026, 12, 28), date(2027, 1, 3)), "混合端按显式年裁定"
    _reject({"start": "2027-01-10", "end": "2026-12-20"}), "显式年双端跨年倒序拒绝"
    _reject({"start": "2026-08-10", "end": "08-01"}), "MM-DD 端在显式年已过不另行推断 → 倒序拒"
    assert resolve_when({"start": "2028-02-29", "end": "2028-03-01"}, TODAY, SS) == \
        (date(2028, 2, 29), date(2028, 3, 1)), "目标年闰年保留 02-29"
    _reject({"start": "2026-13-01", "end": "2026-08-20"}), "显式非法日期"
    print("  OK test_rw_start_end")


def test_rw_structural_rejects():
    """值级/结构拒绝:多键/未知键/空 when/None → ambiguous;非法值 → invalid_value"""
    for bad in ({}, None, {"date": "2026-08-20", "days": 2}, {"when2": 1},
                {"date": "2026-08-20", "hack": 1}):
        try:
            resolve_when(bad, TODAY, SS)
            raise AssertionError(f"应拒绝 {bad!r}")
        except ResolveReject as e:
            assert e.category == "ambiguous", f"{bad!r} → {e.category}"
    for bad in ({"date": "13-40"}, {"date": "02-30"}, {"date": "8-20"}, {"date": 5}):
        try:
            resolve_when(bad, TODAY, SS)
            raise AssertionError(f"应拒绝 {bad!r}")
        except ResolveReject as e:
            assert e.category == "invalid_value", f"{bad!r} → {e.category}"
    print("  OK test_rw_structural_rejects")


# ═══ api 层形态约束/分端点/学期边界/move 源槽(Task 6 增补)═══

def _api(td, cfg=None, today=TODAY):
    return ScheduleApi(td, cfg if cfg is not None else _cfg(), today=today)


def test_api_shape_constraints():
    # 形态约束先于学期边界(单日 kind 收区间 → shape_mismatch,与学期状态无关)
    with tempfile.TemporaryDirectory() as td:
        api = _api(td, cfg=_cfg(semester_end="2027-01-01"))  # 延长学期:exam_week×week_offset 用例不被学期后拒绝
        # 单日 kind 收区间形态 → shape_mismatch("下周交材料"落在 reminder → 追问)
        try:
            api.apply_override({"kind": "reminder", "when": {"week_offset": 1}, "label": "交材料"})
            raise AssertionError("reminder 收区间应拒绝")
        except ApiRejection as e:
            assert e.category == "shape_mismatch"
        try:
            api.apply_override({"kind": "reminder", "when": {"start": "2026-08-10", "end": "2026-08-14"}, "label": "x"})
            raise AssertionError("reminder 收 start-end 应拒绝")
        except ApiRejection as e:
            assert e.category == "shape_mismatch"
        # cancel 区间 → date+end_date("下周停课一周"可达,C3 死锁修复)
        r = api.apply_override({"kind": "cancel", "when": {"week_offset": 1}, "period": 3, "note": "停课"})
        it = r["item"]
        assert it["date"] == "2026-08-10" and it["end_date"] == "2026-08-14", f"got {it}"
        # exam_week 收全部形态:week_offset 区间 → 周一/周五端点(二十轮点名)
        r = api.apply_override({"kind": "exam_week", "when": {"week_offset": 0}, "label": "期末"})
        assert r["item"]["date"] == "2026-08-03" and r["item"]["end_date"] == "2026-08-07", f"got {r['item']}"
        # exam_week 单日形态 → date=end_date 退化(F-D)
        r = api.apply_override({"kind": "exam_week", "when": {"weekday": 3}, "label": "期末2"})
        assert r["item"]["date"] == r["item"]["end_date"] == "2026-08-12", f"got {r['item']}"
    print("  OK test_api_shape_constraints")


def test_api_to_date_forms():
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_cache.json").write_text(json.dumps(
            {"schedule": {"0": {"3": {"course": "高数", "teacher": "刘洋",
               "weeks": [24, 25], "weeks_raw": "24-25周", "location": "A301", "alternates": []}},
                           "3": {"3": {"course": "高数", "teacher": "刘洋",
               "weeks": [24], "weeks_raw": "第24周", "location": "A301", "alternates": []}}}}))
        api = _api(td)
        # 跨天 move:when 单日 + to_date 五态(HIGH-3;"明天的课调到下周五")
        r = api.apply_override({"kind": "move", "when": {"days": 1}, "period": 3, "to_period": 5,
                                "to_date": {"week_offset": 1, "weekday": 5}})
        assert r["item"]["date"] == "2026-08-06" and r["item"]["to_date"] == "2026-08-14", f"got {r['item']}"
        # to_date 不收 week_offset 单 / start-end(C2/M4)
        for bad_td in ({"week_offset": 1}, {"start": "2026-08-10", "end": "2026-08-14"}):
            try:
                api.apply_override({"kind": "move", "when": {"date": "2026-08-06"}, "period": 3,
                                    "to_period": 5, "to_date": bad_td})
                raise AssertionError(f"to_date 应收区间拒绝: {bad_td}")
            except ApiRejection as e:
                assert e.category == "shape_mismatch"
        # to_date 边界三态(二十轮点名):倒序拒/无变化拒/同天换节次允许
        try:
            api.apply_override({"kind": "move", "when": {"date": "2026-08-10"}, "period": 3,
                                "to_period": 5, "to_date": {"date": "2026-08-07"}})
            raise AssertionError("to_date < date 应拒绝")
        except ApiRejection as e:
            assert e.category == "shape_mismatch"
        try:
            api.apply_override({"kind": "move", "when": {"date": "2026-08-10"}, "period": 3,
                                "to_period": 3, "to_date": {"date": "2026-08-10"}})
            raise AssertionError("无变化应拒绝")
        except ApiRejection as e:
            assert e.category == "shape_mismatch"
        r = api.apply_override({"kind": "move", "when": {"date": "2026-08-10"}, "period": 3,
                                "to_period": 5, "to_date": {"date": "2026-08-10"}})
        assert r["item"]["to_date"] == "2026-08-10", "同天换节次允许"
        # to_date 过去 → past_date
        try:
            api.apply_override({"kind": "move", "when": {"date": "2026-08-10"}, "period": 3,
                                "to_period": 5, "to_date": {"date": "2026-08-03"}})
            raise AssertionError("to_date 过去应拒绝")
        except ApiRejection as e:
            assert e.category == "past_date"
    print("  OK test_api_to_date_forms")


def test_api_past_checks_by_kind():
    """分端点过去校验(十六轮 F1/C3):课程例外与区间事实查 end;单日与 to_date 查 date"""
    with tempfile.TemporaryDirectory() as td:
        api = _api(td, cfg=_cfg(semester_end="2027-01-01"))
        # 单日过去 → past_date;今天本身 → 通过(严格 < today)
        try:
            api.apply_override({"kind": "reminder", "when": {"date": "2026-08-03"}, "label": "x"})
            raise AssertionError("过去单日应拒绝")
        except ApiRejection as e:
            assert e.category == "past_date"
        r = api.apply_override({"kind": "reminder", "when": {"date": "2026-08-05"}, "label": "今天"})
        assert r["item"]["date"] == "2026-08-05"
        # 区间性 cancel:start 已过但 end ≥ today → 通过(查 end;"本周停课"周三说生效)
        r = api.apply_override({"kind": "cancel", "when": {"date": "2026-08-03"}, "end_date": "2026-08-07",
                                "period": 3})
        assert r["item"]["end_date"] == "2026-08-07"
        # end 已过 → past_date(已结束区间拒绝)
        try:
            api.apply_override({"kind": "cancel", "when": {"date": "2026-07-27"}, "end_date": "2026-07-31",
                                "period": 3})
            raise AssertionError("已结束区间应拒绝")
        except ApiRejection as e:
            assert e.category == "past_date"
        # exam_week 进行中 end ≥ today → 通过
        r = api.apply_override({"kind": "exam_week", "when": {"date": "2026-08-03"}, "end_date": "2026-08-09",
                                "label": "期末"})
        assert r["item"]["date"] == "2026-08-03"
        # {0, 已过星期} → 过去日期拒绝(组合是显式周指定,非"已过"语义)
        try:
            api.apply_override({"kind": "reminder", "when": {"week_offset": 0, "weekday": 2}, "label": "x"})
            raise AssertionError("{0,已过} 应拒")
        except ApiRejection as e:
            assert e.category == "past_date"
    print("  OK test_api_past_checks_by_kind")


def test_api_semester_boundary():
    """学期前/后 week_offset(非 cancel 类拒绝追问,二十轮对称化;cancel no-op 自然空)"""
    with tempfile.TemporaryDirectory() as td:
        api = _api(td, cfg=_cfg(semester_start="2026-09-01"))
        try:
            api.apply_override({"kind": "exam_week", "when": {"week_offset": 0}, "label": "期末"})
            raise AssertionError("学期前 week_offset 应拒绝")
        except ApiRejection as e:
            assert e.category == "before_semester"
        with tempfile.TemporaryDirectory() as td2:
            api2 = _api(td2)  # semester_start 2026-02-23,semester_end 2026-07-04
            try:
                api2.apply_override({"kind": "exam_week", "when": {"week_offset": 0}, "label": "期末"})
                raise AssertionError("学期后目标周超学期周数应拒绝")
            except ApiRejection as e:
                assert e.category == "after_semester"
            r = api2.apply_override({"kind": "cancel", "when": {"week_offset": 0}, "period": 3})
            assert r["ok"] is True, "cancel 保持 no-op 语义(自然空匹配),不拒绝"
    print("  OK test_api_semester_boundary")


def test_api_move_source_and_snapshot():
    """move 源槽参照系 = 基底课表 + 已应用 add 例外;add weeks 快照派生(M7)"""
    cache = {"schedule": {"2": {"3": {"course": "高数", "teacher": "刘洋", "weeks": [2, 26],
                                      "weeks_raw": "第2周", "location": "A301", "alternates": []}}}}
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_cache.json").write_text(json.dumps(cache))
        api = _api(td)
        # 源槽无课(第 24 周周三无课)→ no_source_class
        try:
            api.apply_override({"kind": "move", "when": {"date": "2026-08-05"}, "period": 3,
                                "to_period": 7})
            raise AssertionError("源槽无课应拒绝")
        except ApiRejection as e:
            assert e.category == "no_source_class"
        # 基底有课 → 通过,且快照 weeks=[当日周次](M7);08-19 未来(相对 api today 08-05)→ 过过去校验
        r = api.apply_override({"kind": "move", "when": {"date": "2026-08-19"}, "period": 3,
                                "to_period": 7, "course": {"course": "高数"}})
        c = r["item"]["course"]
        assert c["weeks"] == [26] and c["weeks_raw"] == "第26周" and c["alternates"] == [], f"快照派生, got {c}"
        # 已应用 add 例外提供源槽 → 通过
        api.apply_override({"kind": "add", "when": {"date": "2026-08-05"}, "period": 9,
                            "course": {"course": "晚自习", "location": "自习室"}})
        r = api.apply_override({"kind": "move", "when": {"date": "2026-08-05"}, "period": 9,
                                "to_period": 7, "course": {"course": "晚自习"}})
        assert r["ok"] is True, "源槽来自 add 例外 → 允许"
    print("  OK test_api_move_source_and_snapshot")

if __name__ == "__main__":
    print("test_day_plan.py\n")
    tests = [test_week_number_monday_alignment, test_week_courses_active_and_alternates,
             test_day_plan_no_files_on_read,
             test_resolve_classes_move_dual_slot, test_availability_tiers_and_overlap,
             test_class_load_adjust,
             test_rw_explicit_and_mmdd, test_rw_days, test_rw_weekday,
             test_rw_week_offset_interval, test_rw_combo, test_rw_start_end,
             test_rw_structural_rejects, test_api_shape_constraints, test_api_to_date_forms,
             test_api_past_checks_by_kind, test_api_semester_boundary,
             test_api_move_source_and_snapshot]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
