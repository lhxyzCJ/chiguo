#!/usr/bin/env python3
"""test_recall.py — 回忆接口单元测试(批次 2c)"""

import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date
from pathlib import Path

TODAY = date(2026, 8, 5)

from schedule.sources import load_sources
from schedule.recall import recall

CFG = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04",
                    "special_dates": ["05-11"], "exam_weeks": []}}


def _mk(td, anniv=None, ovr=None):
    if anniv is not None:
        Path(td, "anniversaries.json").write_text(json.dumps({"anniversaries": anniv}, ensure_ascii=False))
    if ovr is not None:
        Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": ovr}, ensure_ascii=False))
    return load_sources(td, CFG)


def test_recall_by_date_window():
    """日期查询 ±7 天窗口;窗口外不召回;跨年 move 目标日不召回(HIGH-2 注记)"""
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, ovr=[
            {"id": "r1", "date": "2026-08-20", "kind": "reminder", "label": "交材料",
             "created_at": "2026-08-01T10:00:00+08:00"},
            {"id": "r2", "date": "2026-08-28", "kind": "reminder", "label": "远提醒",
             "created_at": "2026-08-01T10:00:00+08:00"},
            {"id": "c1", "date": "2026-08-12", "kind": "cancel", "period": 3, "note": "临时停课",
             "created_at": "2026-08-01T10:00:00+08:00"},
            {"id": "m1", "date": "2026-08-03", "to_date": "2026-08-20", "kind": "move",
             "period": 3, "to_period": 5, "course": {"course": "高数"},
             "created_at": "2026-08-01T10:00:00+08:00"}])
        r = recall("2026-08-20", src, TODAY)
        labels = {(m["type"], m.get("label", "")) for m in r["matches"]}
        assert ("reminder", "交材料") in labels, f"got {labels}"
        assert ("reminder", "远提醒") not in labels, "8/28 超出 ±7 不召回"
        assert ("override", "临时停课") not in labels, "8/12 超出 ±7 不召回"
        assert not any(m["type"] == "override" and m.get("label") == "高数" for m in r["matches"]), \
            "跨天 move 目标日不召回(HIGH-2)"
        assert r["no_match"] is False
    print("  OK test_recall_by_date_window")


def test_recall_by_keyword_full_scan():
    """关键词查询全量子串扫描不截窗口(F10);exam_week label 命中(F11);截断 ≤20"""
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, anniv=[
            {"id": "a1", "type": "anniversary", "name": "哥哥的生日", "date": "05-11",
             "note": "", "created_at": "2026-01-01"},
            {"id": "a2", "type": "anniversary", "name": "认识纪念日", "date": "07-08",
             "note": "", "created_at": "2026-01-01"}],
            ovr=[
                {"id": "e1", "date": "2026-01-05", "end_date": "2026-01-11", "kind": "exam_week",
                 "label": "期末考试周", "created_at": "2026-01-01T10:00:00+08:00"}])
        r = recall("考试周", src, TODAY)
        assert any(m["type"] == "override" and "考试周" in m.get("label", "") for m in r["matches"]), \
            f"F11:exam_week label 子串命中, got {r['matches']}"
        r = recall("生日", src, TODAY)
        assert any("生日" in m.get("label", "") for m in r["matches"]), f"全量子串扫描(1月事实也命中), got {r['matches']}"
        assert r["no_match"] is False
        # 无匹配 → 显式信号(agent 反问引导)
        r = recall("不存在的关键词xyz", src, TODAY)
        assert r["matches"] == [] and r["no_match"] is True, f"got {r}"
        # 截断 ≤ 20
        items = [{"id": f"r{i}", "date": "2026-08-20", "kind": "reminder", "label": f"标签{i}",
                  "created_at": "2026-08-01T10:00:00+08:00"} for i in range(25)]
        with tempfile.TemporaryDirectory() as td2:
            src2 = _mk(td2, ovr=items)
            r = recall("标签", src2, TODAY)
            assert len(r["matches"]) <= 20, f"matches 截断 ≤20, got {len(r['matches'])}"
    print("  OK test_recall_by_keyword_full_scan")


def test_recall_chinese_date_query():
    """中文日期形态 '8月20日' 同走日期查询"""
    with tempfile.TemporaryDirectory() as td:
        src = _mk(td, ovr=[
            {"id": "r1", "date": "2026-08-20", "kind": "reminder", "label": "交材料",
             "created_at": "2026-08-01T10:00:00+08:00"}])
        r = recall("8月20日", src, TODAY)
        assert any(m.get("label") == "交材料" for m in r["matches"])
    print("  OK test_recall_chinese_date_query")


if __name__ == "__main__":
    print("test_recall.py\n")
    tests = [test_recall_by_date_window, test_recall_by_keyword_full_scan,
             test_recall_chinese_date_query]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
