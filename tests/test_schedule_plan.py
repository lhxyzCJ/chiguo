#!/usr/bin/env python3
"""test_schedule_plan.py — replan 链路:触发矩阵/跳过/校验/TOCTOU/悬挂(批次 7)"""

import json, os, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from schedule.replan import (check_dirty, should_skip, validate_plan,
                             replan_env, replan_timeout)

CFG = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04"}}


def _mk(td, plan=None, ovr=None):
    if plan is not None:
        Path(td, "schedule_plan.json").write_text(json.dumps(plan))
    if ovr is not None:
        Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": ovr}))
    return td


def test_dirty_matrix():
    """mtime 文件集合 = overrides+holidays 仅此二者;break_state/schedule_cache 变更不触发(二十轮点名)"""
    with tempfile.TemporaryDirectory() as td:
        _mk(td, plan={"plan_version": 1, "generated_at": "2026-08-03T15:00:00+08:00", "modifiers": []},
            ovr=[{"id": "e1", "date": "2026-08-03", "end_date": "2026-08-09", "kind": "exam_week",
                  "label": "期末", "created_at": "2026-08-01T10:00:00+08:00"}])
        # R4:overrides mtime 钉到过去(generated_at 之前)→ 首断言与墙钟无关
        past = datetime(2026, 8, 3, 10, 0, tzinfo=timezone(timedelta(hours=8))).timestamp()
        os.utime(Path(td, "schedule_overrides.json"), (past, past))
        assert check_dirty(td, CFG) is False, "overrides mtime 早于 generated_at → 不脏"
        time.sleep(0.01)
        Path(td, "schedule_overrides.json").touch()
        assert check_dirty(td, CFG) is True, "overrides mtime 新 → 脏"
        _mk(td, plan=None, ovr=[])
        Path(td, "break_state.json").write_text("{}")
        Path(td, "schedule_cache.json").write_text("{}")
        assert check_dirty(td, CFG) is True, "plan 缺失 → 脏"
        _mk(td, plan={"plan_version": 1, "generated_at": "2099-01-01T00:00:00+08:00", "modifiers": []}, ovr=[])
        Path(td, "break_state.json").write_text("{}")
        Path(td, "schedule_cache.json").write_text("{}")
        assert check_dirty(td, CFG) is False, "break_state/schedule_cache 变更不触发(仅 overrides+holidays)"
    print("  OK test_dirty_matrix")


def test_skip_and_validate():
    """跳过条件:无区间事实且无当年日历的节假日(R1);校验:≤20/clamp/未知 ref/未知字段"""
    with tempfile.TemporaryDirectory() as td:
        from schedule.sources import load_sources
        src = load_sources(td, CFG)
        assert should_skip(src, date(2027, 1, 1)) is True, "无区间事实且无 2027 当年日历 → 跳过"
        Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": [
            {"id": "e1", "date": "2026-08-03", "end_date": "2026-08-09", "kind": "exam_week",
             "label": "期末", "created_at": "2026-08-01T10:00:00+08:00"}]}))
        assert should_skip(load_sources(td, CFG), date(2026, 8, 5)) is False, "有区间事实 → 不跳过"
        Path(td, "schedule_overrides.json").unlink()
        # 节假日(内嵌 2026 国庆)→ 当年日历存在 → 不跳过(holiday 是合法 ref 源)
        assert should_skip(load_sources(td, CFG), date(2026, 8, 5)) is False, "有当年节假日 → 不跳过"
    with tempfile.TemporaryDirectory() as td:
        from schedule.sources import load_sources
        src = load_sources(td, CFG)
        errs = validate_plan({"modifiers": [{"ref": "fact:bad", "trigger_scale": {"special": 0.5}}]}, src)
        assert any("ref" in e for e in errs), f"未知 ref 拒, got {errs}"
        errs = validate_plan({"modifiers": [{"ref": "holiday:国庆节", "trigger_scale": {"xxx": 0.5}}]}, src)
        assert any("类型" in e for e in errs), f"未知类型名拒, got {errs}"
        errs = validate_plan({"modifiers": [{"ref": "holiday:国庆节", "trigger_scale": {"special": 20.0}}]}, src)
        assert any("clamp" in e or "0.1" in e for e in errs), f"clamp 拒, got {errs}"
        errs = validate_plan({"modifiers": [{"ref": "holiday:国庆节", "trigger_scale": {"special": 1.0}, "hack": 1}]}, src)
        assert any("未知字段" in e for e in errs), f"modifier 未知字段拒, got {errs}"
        errs = validate_plan({"modifiers": [{"ref": f"holiday:国庆节{i}", "trigger_scale": {"special": 1.0}}
                                            for i in range(21)]}, src)
        assert any("20" in e for e in errs), f"modifiers > 20 拒, got {errs}"
    print("  OK test_skip_and_validate")


def test_replan_pi_env_and_timeout():
    """replan 的 agent 子进程环境/超时:独立 thinking 档位(默认 high,不被 [host] thinking=max 拖垮)
    与可配超时(默认 240s,下限 60s)。生产机实机:120s 硬编码 + thinking=max 必超时(F9);
    agent-run.mjs 内层超时读 AGENTRUN_TIMEOUT,须与 replan_timeout 同步,否则外层超时是死旋钮。"""
    # 档位优先级:CHIGUO_REPLAN_THINKING > 环境 AGENTRUN_THINKING > 默认 high
    assert replan_env({})["AGENTRUN_THINKING"] == "high", "无配置 → 默认 high"
    assert replan_env({"AGENTRUN_THINKING": "max"})["AGENTRUN_THINKING"] == "max", "尊重显式 AGENTRUN_THINKING"
    assert replan_env({"CHIGUO_REPLAN_THINKING": "medium"})["AGENTRUN_THINKING"] == "medium", \
        "replan 专用档位生效"
    assert replan_env({"CHIGUO_REPLAN_THINKING": "medium", "AGENTRUN_THINKING": "max"})["AGENTRUN_THINKING"] == "medium", \
        "replan 专用档位优先于环境 AGENTRUN_THINKING"
    e = replan_env({"A": "b"})
    assert e["A"] == "b", "其余环境变量保留"
    # 超时:默认 240,env 覆盖,非法值兜底;AGENTRUN_TIMEOUT 同步注入(agent-run.mjs 内层超时)
    assert replan_timeout({}) == 240, "默认 240s"
    assert replan_timeout({"CHIGUO_REPLAN_TIMEOUT": "500"}) == 500, "env 覆盖超时"
    assert replan_timeout({"CHIGUO_REPLAN_TIMEOUT": "abc"}) == 240, "非法值兜底"
    assert replan_timeout({"CHIGUO_REPLAN_TIMEOUT": "30"}) >= 60, "下限 60s"
    assert replan_env({})["AGENTRUN_TIMEOUT"] == "240", "AGENTRUN_TIMEOUT 同步默认"
    assert replan_env({"CHIGUO_REPLAN_TIMEOUT": "500"})["AGENTRUN_TIMEOUT"] == "500", "AGENTRUN_TIMEOUT 同步覆盖值"
    print("  OK test_replan_pi_env_and_timeout")
