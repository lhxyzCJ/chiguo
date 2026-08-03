#!/usr/bin/env python3
"""test_availability.py — availability 三层重组:档位矩阵 + 数值链 + 重叠优先级(批次 3a)"""

import json, os, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tomllib
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState
from schedule.day_plan import bayesian_adjust

CACHE = {"cache_version": 2, "parsed_at": 0, "schedule": {
    "2": {"3": {"course": "高数", "teacher": "刘洋", "weeks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
                "weeks_raw": "2-17周", "location": "A301", "alternates": []},
          "5": {"course": "线代", "teacher": "王芳", "weeks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
                "weeks_raw": "2-17周", "location": "B202", "alternates": []}},
    "0": {"1": {"course": "英语", "teacher": "赵敏", "weeks": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
                "weeks_raw": "2-17周", "location": "C303", "alternates": []}}}}


def make_state(td, cfg):
    src = Path("chiguo_proactive.toml").read_text()
    tmp_toml = Path(td) / "chiguo_proactive_test.toml"
    import re as _re
    src = _re.sub(r"(?m)^lancedb_path\s*=.*$", f'lancedb_path = "{Path(td) / "no_lancedb"}"', src)
    tmp_toml.write_text(src)
    with open(tmp_toml, "rb") as f:
        c = tomllib.load(f)
    c["_base_dir"] = td
    for k, v in cfg.items():
        if k == "schedule":
            c["schedule"].update(v)
        else:
            c[k] = v
    return ChiguoState(c)


def dt(y, mo, d, h=12, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=CST)


def _avail(td, cfg, now, ovr=None, breaks=None, cache=None):
    if ovr is not None:
        Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": ovr}))
    if breaks is not None:
        Path(td, "break_state.json").write_text(json.dumps(breaks, ensure_ascii=False))
    if cache is not None:
        Path(td, "schedule_cache.json").write_text(json.dumps(cache))
    s = make_state(td, cfg)
    return s.availability(now)


def test_tier_matrix():
    """五档 tier:break/exam/non_school/unavailable/idle_school"""
    cfg_ok = {"schedule": {"enabled": True, "semester_start": "2026-02-23",
                           "semester_end": "2099-12-31", "exam_weeks": []}}
    with tempfile.TemporaryDirectory() as td:
        # unavailable:enabled 但无缓存数据 → 1.0
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 14, 0)) == 1.0, "课表不可用 → 1.0"
    with tempfile.TemporaryDirectory() as td:
        # enabled=false + 陈旧缓存 → 课表未启用(unavailable 1.0,spec 未启用判据)
        cfg_off = {"schedule": {"enabled": False, "semester_start": "2026-02-23",
                                "semester_end": "2099-12-31", "exam_weeks": []}}
        assert _avail(td, cfg_off, dt(2026, 3, 4, 14, 0), cache=CACHE) == 1.0, "enabled=false → 1.0"
    with tempfile.TemporaryDirectory() as td:
        # idle_school 上课中:周三 14:00 第 5 节,2 节课 → light 0.12
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 14, 0), cache=CACHE) == 0.12, "2 节课 → light 0.12"
    with tempfile.TemporaryDirectory() as td:
        # 课间(09:36,第 3/5 节均未开始)→ 剩余 2 节 → 0.50
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 9, 36), cache=CACHE) == 0.50, "剩余 2 节"
    with tempfile.TemporaryDirectory() as td:
        # 12:00 课间:第 5 节 14:00 未开始,剩余 1 节 → 0.70
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 12, 0), cache=CACHE) == 0.70, "12:00 课间:第 5 节 14:00 未开始,剩余 1 节 → 0.70"
    with tempfile.TemporaryDirectory() as td:
        # 考试周 → 0.5
        ovr = [{"id": "e1", "date": "2026-03-02", "end_date": "2026-03-06", "kind": "exam_week",
                "label": "期末", "created_at": "2026-03-01T10:00:00+08:00"}]
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 14, 0), ovr=ovr, cache=CACHE) == 0.5, "考试周 → 0.5"
    with tempfile.TemporaryDirectory() as td:
        # 法定节假日 → 0.85
        assert _avail(td, cfg_ok, dt(2026, 10, 1, 14, 0), cache=CACHE) == 0.85, "节假日 → 0.85"
    with tempfile.TemporaryDirectory() as td:
        # 周末 → 0.85
        assert _avail(td, cfg_ok, dt(2026, 3, 7, 14, 0), cache=CACHE) == 0.85, "周末 → 0.85"
    with tempfile.TemporaryDirectory() as td:
        # 寒暑假手动开关 → 0.85
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 14, 0), breaks={"manual_override": True, "breaks": []},
                      cache=CACHE) == 0.85, "手动寒暑假 → 0.85"
    print("  OK test_tier_matrix")


def test_exam_overlap_priority():
    """C5 重叠优先级(二十轮落点):考周×周末 → 0.5;×节假日 → 0.85;×寒暑假 → 0.85"""
    cfg_ok = {"schedule": {"enabled": True, "semester_start": "2026-02-23",
                           "semester_end": "2099-12-31", "exam_weeks": []}}
    ovr = [{"id": "e1", "date": "2026-03-02", "end_date": "2026-03-08", "kind": "exam_week",
            "label": "期末", "created_at": "2026-03-01T10:00:00+08:00"}]  # 覆盖周六 3/7
    with tempfile.TemporaryDirectory() as td:
        assert _avail(td, cfg_ok, dt(2026, 3, 7, 14, 0), ovr=ovr, cache=CACHE) == 0.5, "考周×周末 → 0.5"
    with tempfile.TemporaryDirectory() as td:
        ovr2 = [{"id": "e2", "date": "2026-09-28", "end_date": "2026-10-02", "kind": "exam_week",
                 "label": "期末", "created_at": "2026-09-01T10:00:00+08:00"}]
        assert _avail(td, cfg_ok, dt(2026, 10, 1, 14, 0), ovr=ovr2, cache=CACHE) == 0.85, "考周×节假日 → 0.85"
    with tempfile.TemporaryDirectory() as td:
        breaks = {"breaks": [{"start": "2026-03-01", "end": "2026-03-31", "note": "春假"}]}
        assert _avail(td, cfg_ok, dt(2026, 3, 7, 14, 0), ovr=ovr, breaks=breaks, cache=CACHE) == 0.85, "考周×寒暑假 → 0.85"
    print("  OK test_exam_overlap_priority")


def test_cancel_changes_availability():
    """例外取消 → remaining 减少 → availability 上升(例外生效由此体现,§5.1)"""
    cfg_ok = {"schedule": {"enabled": True, "semester_start": "2026-02-23",
                           "semester_end": "2099-12-31", "exam_weeks": []}}
    with tempfile.TemporaryDirectory() as td:
        # 周三 09:36 课间:第 3/5 节都在 → 剩余 2 → 0.50
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 9, 36), cache=CACHE) == 0.50
        # 取消第 5 节(区间性 cancel 覆盖周三)后 → 剩余 1 → 0.70
        ovr = [{"id": "c1", "date": "2026-03-02", "end_date": "2026-03-06", "kind": "cancel",
                "period": 5, "note": "本周停课", "created_at": "2026-03-01T10:00:00+08:00"}]
        assert _avail(td, cfg_ok, dt(2026, 3, 4, 9, 36), ovr=ovr, cache=CACHE) == 0.70, "cancel 后 0.50→0.70"
    print("  OK test_cancel_changes_availability")


def test_bayesian_layer():
    """第三层:高置信 sleeping → 0.0;busy ×0.5;needs_care min(×1.2,0.95);anxiety ×0.3"""
    class _E:
        anxiety = 40.0
    cfg = {"cooldown": {"anxiety_block_threshold": 70.0},
           "bayesian": {"min_confidence_for_block": 0.5}}
    assert bayesian_adjust(0.85, {"most_likely": "sleeping", "confidence": 0.9}, _E(), cfg) == 0.0
    assert bayesian_adjust(0.85, {"most_likely": "sleeping", "confidence": 0.1}, _E(), cfg) == 0.85, "低置信不误伤"
    assert abs(bayesian_adjust(0.85, {"most_likely": "busy", "confidence": 0.9}, _E(), cfg) - 0.425) < 1e-9
    assert abs(bayesian_adjust(0.85, {"most_likely": "needs_care", "confidence": 0.9}, _E(), cfg) - 0.95) < 1e-9, "min(×1.2,0.95)"
    assert abs(bayesian_adjust(0.5, {"most_likely": "browsing", "confidence": 0.9},
                               type("E", (), {"anxiety": 80.0})(), cfg) - 0.15) < 1e-9, "anxiety 阻塞 ×0.3"
    print("  OK test_bayesian_layer")


def test_lambda_monotonic():
    """数值链:current_lambda 随 availability 单调(满课 < 空闲;event_timestamps 空,anxiety<70)"""
    cfg_ok = {"schedule": {"enabled": True, "semester_start": "2026-02-23",
                           "semester_end": "2099-12-31", "exam_weeks": []}}
    with tempfile.TemporaryDirectory() as td:
        Path(td, "schedule_cache.json").write_text(json.dumps(CACHE))
        s = make_state(td, cfg_ok)
        s.cooldown.event_timestamps = []          # Hawkes 空(加法项置零)
        s.cooldown.messages_without_reply = 0     # 退避系数恒 1
        s.cooldown.accumulated_lambda = 0.0
        s.emotion.anxiety = 40.0                  # < 70 阻塞阈值外
        s._bayesian_estimator = None
        s.emotion.loneliness_rate = 0.0
        s.emotion.anxiety_rate = 0.0
        lam_in_class = s.current_lambda(dt(2026, 3, 4, 14, 0))
        lam_free = s.current_lambda(dt(2026, 3, 4, 12, 0))
        assert lam_in_class < lam_free, f"满课 λ 应低于空闲: {lam_in_class} vs {lam_free}"
        assert lam_in_class > 0
        # 考试周再降(0.5 < 0.85)
        s.override_store.add({"kind": "exam_week", "date": "2026-03-02",
                              "end_date": "2026-03-06", "label": "期末"},
                             datetime(2026, 3, 1, 10, 0, tzinfo=CST))
        s._rc_cache = {}   # 强制重载:缓存按日期键控,写后需失效,否则考试周断言空转
        lam_exam = s.current_lambda(dt(2026, 3, 4, 14, 0))
        assert lam_exam < lam_free, f"考试周 λ 低于空闲: {lam_exam} vs {lam_free}"
    print("  OK test_lambda_monotonic")


if __name__ == "__main__":
    print("test_availability.py\n")
    tests = [test_tier_matrix, test_exam_overlap_priority, test_cancel_changes_availability,
             test_bayesian_layer, test_lambda_monotonic]
    for t in tests:
        t()
    print(f"\n{'='*40}\nALL {len(tests)} tests passed.")
