#!/usr/bin/env python3
"""test_weeks_serialization.py — F-A22-001 回归护栏：学期课程周决策 JSON 序列化（R1 时间炸弹）。

Bug: schedule/parser.py:126 把 course weeks 转成 set → day_plan.week_courses 原样透传
(set weeks 的 course) → attention.t3_window(this_week∪next_week) → chiguo_state.snapshot()
["attention"] 无条件注入 → decision/core._log 的 json.dumps(decision) 抛
TypeError: Object of type set is not JSON serializable。
1) 决策 JSONL 全量静默写失败（仅 stderr warn，30 天模拟 615 次失败）；
2) `--compact` 模式下 send 决策走 cli/dispatch.py:201 print(json.dumps(decision)) 抛未捕获
   TypeError → 进程崩溃 → tick.sh 整链失败。

护栏（自包含，不依赖审计目录）：
- 用生产 data/xskb.xlsx 副本 + 隔离 tempdir base_dir + CHIGUO_MEM0_DISABLED=1 构造真实课表。
- 课程周日期固定为 2026-06-16（第 17 周，1-17 周覆盖内 → week_courses 返回含 set weeks 的课程）。
- snapshot → json.dumps 必须成功（回归红线）。
- 完整链：固定时钟 → DecisionEngine.evaluate() → 决策 JSONL 实际写入成功。
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

# 课程周:2026-06-16 是第 17 周（周一），在 xskb 课程 weeks 覆盖 1-17 内 →
# week_courses 在该日返回含（set）weeks 的课程。8 月(week 25)无课程匹配为对照组。
COURSE_WEEK = datetime(2026, 6, 16, 10, 0, tzinfo=CST)   # 第 17 周
SUMMER_WEEK = datetime(2026, 8, 12, 10, 0, tzinfo=CST)   # 第 25 周（无课程）


def setup_base_dir(prefix: str) -> Path:
    """建隔离 base_dir：复制 config（mem0/netease 隔离）+ 生产 xskb.xlsx 副本。"""
    os.environ.setdefault("CHIGUO_MEM0_DISABLED", "1")
    base = Path(tempfile.mkdtemp(prefix=prefix))
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{base / "data/mem0/qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{base / "data/mem0/history.db"}"', src)
    lines, in_net = [], False
    for ln in src.splitlines(keepends=True):
        if ln.startswith("[netease]"):
            in_net = True
        elif ln.startswith("[") and in_net:
            in_net = False
        if in_net and re.match(r"^enabled\s*=", ln):
            ln = "enabled = false\n"
        lines.append(ln)
    (base / "chiguo_proactive.toml").write_text("".join(lines))
    xlsx = Path("data/xskb.xlsx")
    if xlsx.exists():
        (base / "data").mkdir(exist_ok=True)
        shutil.copy2(xlsx, base / "data" / "xskb.xlsx")
    return base


def find_set(o, path=""):
    """递归定位任意 set（断言失败时的诊断辅助）。"""
    if isinstance(o, set):
        return path, o
    if isinstance(o, dict):
        for k, v in o.items():
            r = find_set(v, path + "." + str(k))
            if r:
                return r
    elif isinstance(o, list):
        for i, v in enumerate(o):
            r = find_set(v, f"{path}[{i}]")
            if r:
                return r
    return None


# ═══════════════════════════════════════════════════════
# 护栏 1：课程周 snapshot → json.dumps 必须成功
# ═══════════════════════════════════════════════════════

def test_course_week_snapshot_is_json_serializable():
    """第 17 周（课程周）snapshot → json.dumps 成功、且自无 set 残留。

    对照组：第 25 周（无课程）snapshot 本就序列化成功。
    """
    import tomllib
    from chiguo_state import ChiguoState

    base = setup_base_dir("chiguo_test_weeks_ser_snap_")
    try:
        with open(base / "chiguo_proactive.toml", "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(base)
        state = ChiguoState(cfg)

        snap = state.snapshot(COURSE_WEEK)
        # 回归红线：课程周 snapshot 必须可序列化
        json.dumps(snap, ensure_ascii=False)
        # 不应再有任何 set 残留（含 alternates 的 weeks）
        assert find_set(snap) is None, f"snapshot 仍含 set: {find_set(snap)}"

        # 对照组：第 25 周（无课）本来就能序列化
        json.dumps(state.snapshot(SUMMER_WEEK), ensure_ascii=False)
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ═══════════════════════════════════════════════════════
# 护栏 2：完整链 —— 固定时钟 → evaluate() → 决策 JSONL 实际写入
# ═══════════════════════════════════════════════════════

class _Now(datetime):
    """datetime 子类：now() 返回固定时刻（isinstance 判定兼容，见 sim_common.SimClock）。"""
    _current = None

    @classmethod
    def now(cls, tz=None):  # noqa: N805
        return cls._current if cls._current is not None else datetime.now(tz)

    @classmethod
    def at(cls, dt: datetime) -> "_Now":
        return cls(dt.year, dt.month, dt.day, dt.hour, dt.minute,
                   dt.second, dt.microsecond, tzinfo=dt.tzinfo)


def _fixed_clock_modules(now):
    """把 evaluate 路径各模块的 datetime 名字固定为 _Now(now)。"""
    import copy
    import importlib
    _Now._current = _Now.at(now)
    return tuple(
        mock.patch.object(importlib.import_module(m), "datetime", copy.deepcopy(_Now))
        for m in ("decision.core", "ops.engine_ops", "chiguo_state", "chiguo_bayesian")
    )


def test_course_week_decision_jsonl_written():
    """课程周 evaluate() → chiguo_decisions.jsonl 实际写入 ≥1 行（回归红线）。"""
    import decision.core
    import ops.engine_ops
    import chiguo_state
    import chiguo_bayesian

    base = setup_base_dir("chiguo_test_weeks_ser_chain_")
    log_path = base / "chiguo_decisions.jsonl"
    now = _Now.at(COURSE_WEEK)
    _Now._current = now

    patches = (mock.patch.object(decision.core, "datetime", _Now),
               mock.patch.object(ops.engine_ops, "datetime", _Now),
               mock.patch.object(chiguo_state, "datetime", _Now),
               mock.patch.object(chiguo_bayesian, "datetime", _Now))
    for p in patches:
        p.start()
    try:
        from chiguo_daemon import DecisionEngine
        # 构造噪音重定向到 devnull
        devnull = open(os.devnull, "w")
        old_err = sys.stderr
        sys.stderr = devnull
        try:
            eng = DecisionEngine(str(base / "chiguo_proactive.toml"), str(log_path))
        finally:
            sys.stderr = old_err
            devnull.close()
        dec = eng.evaluate()
        assert dec["action"] in ("send", "idle")
    finally:
        for p in patches:
            p.stop()

    assert log_path.exists(), "决策 JSONL 应被创建"
    lines = log_path.read_text().splitlines()
    assert len(lines) == 1, f"evaluate() 应写 1 行决策, got {len(lines)}（写失败静默吞噬）"
    written = json.loads(lines[0])
    assert written["action"] == dec["action"]


# ═══════════════════════════════════════════════════════
# 护栏 3：--compact send 决策输出兜底（default=list）不崩溃
# ═══════════════════════════════════════════════════════

def test_dispatch_send_decision_serialization_fallback():
    """--compact 下 send 决策若仍含 set → print(json.dumps(..., default=list)) 不崩溃。

    兜底路径：cli/dispatch.py _run_passive 的 send 决策经 default=list 序列化，
    而非抛未捕获 TypeError 导致 cron 发送链整链失败（F-A22-001）。
    """
    from cli import dispatch as dmod

    class _FakeEngine:
        _base_dir = Path("/tmp/__no_such_chiguo_fa22__")

        def evaluate(self):
            # 最坏情形：send 决策残留 set（主修复前形态）
            return {"action": "send", "version": "1.19", "msg_id": "m1",
                    "trigger": "morning", "context": {"t3": {1: {3: {"weeks": {2, 3}}}}}}

    engine = _FakeEngine()
    with mock.patch.object(dmod, "startup_conflict", return_value=0), \
         mock.patch("sys.stdout", new_callable=lambda: io.StringIO()) as out, \
         mock.patch.dict(os.environ, {}, clear=False):
        # idle compact 分支单独走 heartbeat；这里是 send → 走 default=list 兜底
        dmod._run_passive(engine, compact=True)
        printed = out.getvalue()
        parsed = json.loads(printed)
        assert parsed["action"] == "send"
        assert parsed["context"]["t3"][str(1)][str(3)]["weeks"] == [2, 3], \
            "set → default=list 序列化成功"
