#!/usr/bin/env python3
"""test_schedule_plan.py — replan 链路:触发矩阵/跳过/校验/TOCTOU/悬挂(批次 7)"""

import json, os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from unittest import mock

from schedule.replan import (check_dirty, should_skip, validate_plan,
                             replan_env, replan_timeout, _run_replan, _lock)

CFG = {"schedule": {"semester_start": "2026-02-23", "semester_end": "2026-07-04"}}


def _mk(td, plan=None, ovr=None):
    if plan is not None:
        Path(td, "schedule_plan.json").write_text(json.dumps(plan))
    if ovr is not None:
        Path(td, "schedule_overrides.json").write_text(json.dumps({"override_version": 1, "items": ovr}))
    return td


def _src(td):
    """真实 sources(空区间事实 + 内嵌节假日 → facts 含 holiday 项的锚定)。"""
    from schedule.sources import load_sources
    return load_sources(td, CFG)


def _fake(res=None, exc=None):
    """mock subprocess.run:返回 res(stdout 文本)或抛 exc。"""
    result = mock.Mock(returncode=0, stdout="", stderr="")
    if res is not None:
        result.stdout = res
    proc = mock.Mock(side_effect=exc) if exc else mock.Mock(return_value=result)
    return mock.patch.object(_run_replan.__globals__["subprocess"], "run", proc), proc


def _assert_agent_call(args, kwargs, env):
    """校验 _run_replan 的 node 调用契约:脚本/参数/env 注入。"""
    assert args[0] == "node", f"node 缺失: {args}"
    assert args[-1] == "--schedule-replan", f"--schedule-replan 缺失: {args}"
    assert args[-3] == "--prompt", f"--prompt 位置错: {args}"
    assert str(args[1]).endswith("scripts/agent-run.mjs"), f"非仓库 agent-run.mjs: {args[1]}"
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True
    assert kwargs.get("env", {}).get("AGENTRUN_THINKING"), f"缺 thinking 注入: {kwargs.get('env', {})}"
    assert kwargs.get("env", {}).get("AGENTRUN_TIMEOUT") == str(replan_timeout(env)), \
        f"AGENTRUN_TIMEOUT 未同步: {kwargs.get('env', {})}"


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


def test_run_replan_success():
    """_run_replan 成功:agent 返回 {ok, parsed} → 原样返回 parsed 计划。"""
    with tempfile.TemporaryDirectory() as td:
        src = _src(td)
        plan = {"modifiers": [{"ref": "holiday:国庆节", "trigger_scale": {"special": 0.6}}]}
        run_patch, proc = _fake(res=json.dumps({"ok": True, "parsed": plan, "raw": "x"}))
        with run_patch:
            out = _run_replan(td, CFG, src)
        assert out == plan, f"成功应返回 parsed, got {out}"
        args, kwargs = proc.call_args.args[0], proc.call_args.kwargs
        _assert_agent_call(args, kwargs, os.environ)
    print("  OK test_run_replan_success")


def test_run_replan_agent_fail():
    """_run_replan 失败:agent 返回 {ok:false} → None(保留旧 plan + stale,_run_replan 层)。"""
    with tempfile.TemporaryDirectory() as td:
        src = _src(td)
        run_patch, _ = _fake(res=json.dumps({"ok": False, "error": "agent 拒绝"}))
        with run_patch:
            assert _run_replan(td, CFG, src) is None, "agent 失败 → None"
        run_patch2, _ = _fake(res=json.dumps({"parsed": {}, "raw": ""}))  # 缺 ok → 非成功
        with run_patch2:
            assert _run_replan(td, CFG, src) is None, "缺 ok 键 → None"
    print("  OK test_run_replan_agent_fail")


def test_run_replan_parse_exception():
    """_run_replan 解析异常:stdout 非法 JSON → None(不崩溃)。"""
    with tempfile.TemporaryDirectory() as td:
        src = _src(td)
        for garbage in ("not json", "", "{}trailing"):
            run_patch, _ = _fake(res=garbage)
            with run_patch:
                assert _run_replan(td, CFG, src) is None, f"非法 JSON {garbage!r} → None"
    print("  OK test_run_replan_parse_exception")


def test_run_replan_timeout():
    """_run_replan 超时:subprocess.TimeoutExpired → None(下轮重试)。"""
    with tempfile.TemporaryDirectory() as td:
        src = _src(td)
        run_patch, _ = _fake(exc=subprocess.TimeoutExpired(["node"], 240))
        with run_patch:
            assert _run_replan(td, CFG, src) is None, "超时 → None"
        run_patch2, _ = _fake(exc=FileNotFoundError("node"))   # node 缺失 → None(防 traceback 刷屏 M5)
        with run_patch2:
            assert _run_replan(td, CFG, src) is None, "node 缺失 → None"
    print("  OK test_run_replan_timeout")


def test_lock_acquire_timeout_takeover():
    """_lock 锁:空目录获锁 true;新鲜锁超时让位 false;陈旧锁(>600s)强制接管 true。"""
    import schedule.replan as rp
    with tempfile.TemporaryDirectory() as td:
        # 无锁 → 获锁 true 并落 lockfile
        assert rp._lock(td) is True, "空目录应获锁"
        assert Path(td, "replan.lock").exists(), "获锁应落 lockfile"

        # 新鲜锁 + 5s 超时 → false(永不接管刚建的锁);mock 时间推进越过 deadline,sleep 空转
        lock = Path(td, "replan.lock")
        lock.write_text("")  # 刷新 mtime 为"新鲜"
        mtime = lock.stat().st_mtime
        counter = {"t": mtime + 100.0}   # age=100s <600 → 新鲜;base+100 → 之后推进越过 +5 deadline
        def fake_time():
            counter["t"] += 0.1
            return counter["t"]
        with mock.patch.object(rp.time, "time", side_effect=fake_time), \
             mock.patch.object(rp.time, "sleep"):
            assert rp._lock(td) is False, "新鲜锁超时 → 让位 false"

        # 陈旧锁(mtime > 600s)→ 接管 true
        old = rp.time.time() - 700
        os.utime(lock, (old, old))
        with mock.patch.object(rp.time, "sleep"):
            assert rp._lock(td) is True, "陈旧锁 → 强制接管 true"
    print("  OK test_lock_acquire_timeout_takeover")
