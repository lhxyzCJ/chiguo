#!/usr/bin/env python3
"""test_attention_tiers.py — 注意力分层 T1/T2/T3 + today_exceptions 单元测试(批次 2c)"""

import json, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import tomllib
from chiguo_state import ChiguoState
import schedule.attention as attn_mod

TODAY = date(2026, 8, 5)
CST = timezone(timedelta(hours=8))

from schedule.sources import load_sources
from schedule.attention import t1_items, t2_block, t3_window, today_exceptions, build_attention

CFG = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2027-01-01",
                    "special_dates": ["05-11"], "exam_weeks": []}}


def _mk(td, anniv=None, ovr=None, breaks=None):
    if anniv is not None:
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": anniv}, ensure_ascii=False))
    if ovr is not None:
        Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": ovr}, ensure_ascii=False))
    if breaks is not None:
        Path(td, "break_state.json").write_text(json.dumps(breaks, ensure_ascii=False))
    return load_sources(td, CFG)


def test_t1_items():
    """T1:当年全部(今天~12-31)、days_until 排序、已过退出/次年重入、reminder 一次性、截断 ≤50"""
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, anniv=[
            {"id": "a1", "type": "anniversary", "name": "迟菓生日", "date": "05-11",
             "note": "", "created_at": "2026-01-01"},        # 已过(2026-05-11 < 08-05)→ 本年不注入
            {"id": "a2", "type": "anniversary", "name": "哥哥的生日", "date": "10-01",
             "note": "", "created_at": "2026-01-01"},
            {"id": "c1", "type": "countdown", "name": "倒计时测试", "date": "2026-08-20",
             "note": "", "created_at": "2026-01-01"}],      # countdown 类型不注入(6c 前保留读取但不进 T1)
            ovr=[
                {"id": "r1", "date": "2026-08-20", "kind": "reminder", "label": "交材料",
                 "created_at": "2026-08-01T10:00:00+08:00"},
                {"id": "r2", "date": "2026-12-31", "kind": "reminder", "label": "年末",
                 "created_at": "2026-08-01T10:00:00+08:00"},
                {"id": "r3", "date": "2027-01-01", "kind": "reminder", "label": "跨年排除",
                 "created_at": "2026-08-01T10:00:00+08:00"}])
        items = t1_items(src, TODAY)
        labels = [(i["kind"], i.get("name") or i.get("label"), i["days_until"]) for i in items]
        assert ("anniversary", "哥哥的生日", 57) in labels, f"got {labels}"
        assert not any(i[1] == "迟菓生日" for i in labels), "已过纪念日本年不注入(次年同日重入)"
        assert ("reminder", "交材料", 15) in labels
        assert ("reminder", "年末", 148) in labels
        assert not any(i[1] == "跨年排除" for i in labels), "提醒日一次性,[today,12-31] 外排除"
        assert not any(i[0] == "countdown" for i in labels)
        assert all(labels[i][2] <= labels[i + 1][2] for i in range(len(labels) - 1)), "days_until 升序"
        # 截断 ≤50(决策 5:toml t1_max_items 默认 50)
        ovr50 = [{"id": f"r{i}", "date": "2026-08-20", "kind": "reminder", "label": f"提醒{i}",
                  "created_at": "2026-08-01T10:00:00+08:00"} for i in range(51)]
        with tempfile.TemporaryDirectory() as td2:
            src2 = _mk(td2, ovr=ovr50)
            assert len(t1_items(src2, TODAY)) == 50, "T1 单轮注入 ≤50"
    print("  OK test_t1_items")


def test_t2_block():
    """T2 五态:放假第X天/还有X天/考试周第X天/manual break/无区间"""
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, ovr=[
            {"id": "e1", "date": "2026-08-03", "end_date": "2026-08-09", "kind": "exam_week",
             "label": "期末考试周", "created_at": "2026-08-01T10:00:00+08:00"}])
        lines = t2_block(src, date(2026, 8, 5))
        assert "今天是期末考试周第3天" in lines, f"第X天公式 (today-start).days+1, got {lines}"
        lines = t2_block(src, date(2026, 8, 1))
        assert "还有2天期末考试周" in lines, f"got {lines}"
        lines = t2_block(src, date(2026, 10, 2))   # 国庆第 2 天(内嵌节假日)
        assert "今天是放国庆节第2天" in lines, f"got {lines}"
        lines = t2_block(src, date(2026, 9, 30))
        assert "还有1天放国庆节" in lines, f"got {lines}"
        # manual break(无日期区间)→ 仅"寒暑假模式中",不生成第X天
        with tempfile.TemporaryDirectory() as td2:
            src2 = _mk(td2, breaks={"manual_override": True, "breaks": []})
            lines2 = t2_block(src2, TODAY)
            assert lines2 == ["寒暑假模式中"], f"got {lines2}"
        # 无区间事实 → 空列表
        with tempfile.TemporaryDirectory() as td3:
            src3 = _mk(td3)
            assert t2_block(src3, TODAY) == []
        # 未来 14 天临近窗口(horizon):15 天外不注入
        lines = t2_block(src, date(2026, 7, 19))
        assert not any("考试周" in l for l in lines), f"15 天外不注入, got {lines}"
    print("  OK test_t2_block")


def test_t3_window_and_today_exceptions():
    """T3 周窗口滚动 + today_exceptions 摘要"""
    cache = {"schedule": {"2": {"3": {"course": "高数", "teacher": "刘洋", "weeks": [24],
                                      "weeks_raw": "第24周", "location": "A301", "alternates": []},
                                  "5": {"course": "线代", "teacher": "王芳", "weeks": [25],
                                        "weeks_raw": "第25周", "location": "B202", "alternates": []}}}}
    with tempfile.TemporaryDirectory() as td:
        src = load_sources(td, CFG, schedule_cache_dict=cache)
        w = t3_window(src, date(2026, 8, 5))  # 第 24 周
        assert w["current_week"] == 24
        assert 3 in w["this_week"][2], "本周含第 24 周课程"
        assert 5 in w["next_week"][2], "下周含第 25 周课程(滚动)"
        ovr = [
            {"id": "c1", "date": "2026-08-05", "kind": "cancel", "period": 3,
             "created_at": "2026-08-01T10:00:00+08:00"},
            {"id": "a1", "date": "2026-08-05", "kind": "add", "period": 9,
             "course": {"course": "晚自习"}, "created_at": "2026-08-01T10:00:00+08:00"},
            {"id": "m1", "date": "2026-08-05", "kind": "move", "period": 3, "to_period": 7,
             "course": {"course": "高数"}, "created_at": "2026-08-01T11:00:00+08:00"}]
        with tempfile.TemporaryDirectory() as td2:
            src2 = _mk(td2, ovr=ovr)
            ex = today_exceptions(src2, TODAY)
            acts = {(e["period"], e["action"]) for e in ex}
            assert (3, "cancel") in acts and (9, "add") in acts and (7, "move") in acts, f"got {ex}"
        # build_attention 汇总形状
        with tempfile.TemporaryDirectory() as td3:
            src3 = _mk(td3)
            b = build_attention(src3, TODAY)
            assert set(b) == {"t1", "t2", "t3", "week_num", "today_exceptions"}, f"got {set(b)}"
            assert b["week_num"] == 24
    print("  OK test_t3_window_and_today_exceptions")


def _mk_state(td):
    """构造临时目录中的 ChiguoState（隔离配置/状态）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(td) / "no_qdrant"}"', src)
    cfg_path = Path(td) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(td)
    return ChiguoState(cfg)


def test_snapshot_attention_m2_cache():
    """M-2 (#230, D5): attention 并入 _rc_cache 按日缓存——同日多次 snapshot 只 build_attention 一次；
    跨日（_resolved_for 整体重建 _rc_cache）→ 重算一次。"""
    import tomllib
    with tempfile.TemporaryDirectory() as td:
        st = _mk_state(td)
        now1 = datetime(2026, 8, 5, 10, 0, tzinfo=CST)
        now1b = datetime(2026, 8, 5, 14, 0, tzinfo=CST)
        now2 = datetime(2026, 8, 6, 9, 0, tzinfo=CST)
        with mock.patch.object(attn_mod, "build_attention",
                               wraps=attn_mod.build_attention) as m:
            st.snapshot(now1)
            st.snapshot(now1b)   # 同日 → 命中 _rc_cache，不再调
            assert m.call_count == 1, f"同日两次 snapshot 应只 build 一次, got {m.call_count}"
            st.snapshot(now2)    # 跨日 → _resolved_for 重建 _rc_cache，重算
            assert m.call_count == 2, f"跨日应重算一次, got {m.call_count}"
            st.snapshot(now2)    # 同日 again → 命中
            assert m.call_count == 2, f"跨日后同日再 snapshot 不重算, got {m.call_count}"
    print("  OK test_snapshot_attention_m2_cache")


if __name__ == "__main__":
    print("test_attention_tiers.py\n")
    tests = [test_t1_items, test_t2_block, test_t3_window_and_today_exceptions,
             test_snapshot_attention_m2_cache]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
