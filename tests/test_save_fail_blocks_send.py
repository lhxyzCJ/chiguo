#!/usr/bin/env python3
"""test_save_fail_blocks_send.py — R15 (#334, F-A18-04): state.save() 失败必须阻断 send 决策

机制（组1 复核 CONFIRMED）：
- evaluate() 中状态变更（msg_id 生成 / on_character_message 标记 / trigger_history
  追加 / reminder 去重标记）全部发生在 save() **之前**；
- save() 返回 False 时 decision/core.py 仅 stderr 告警，仍构建并返回 send 决策；
- scripts/chiguo-tick.sh 对 `--compact` 输出只看 action：
  `[ "$ACTION" = "send" ] || exit 0` → save 失败后消息照常发出，但状态未落盘 →
  下一 cron tick 基于旧状态重新触发（重复消息 / 重复触发）。
- chiguo_state.py tmp 校验失败路径（atomic_write verify 失败，L610-611）返回
  False 且完全无 stderr 告警（比 OSError 路径更静默，审计 M5 修正）。

修复语义（本测试断言）：
- send 路径 save() 返回 False → 不输出 send：转 idle(reason="state_save_failed")
  （tick.sh 对非 send 输出 exit 0 → 发送链被阻断，cron 健康检查语义不变）；
  stderr 必须保留明确告警（可观测）。
- 正常路径零变化：save 成功时 evaluate 仍返回 send（回归护栏）。
- chiguo_state.py tmp 校验失败路径补 warn（M5）。

全部 tempdir 隔离（pytest tmp_path），不触碰真实运行时文件。
"""

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

import decision.core  # noqa: E402
from chiguo_daemon import DecisionEngine  # noqa: E402


def _setup(base: Path) -> Path:
    """复制 toml 到临时目录并锚定 mem0 到 tempdir（隔离 state/log）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{base / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{base / "no_history.db"}"', src)
    cfg_path = base / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return cfg_path


def make_engine(base: Path) -> DecisionEngine:
    cfg_path = _setup(base)
    return DecisionEngine(str(cfg_path), str(base / "chiguo_decisions.jsonl"))


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=CST)


def _fixed_now(now: datetime):
    """把 decision.core 模块内 datetime.now 固定为 now，使 evaluate 全链路确定性。"""
    class _FixedNow(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: N805
            return now
    return mock.patch.object(decision.core, "datetime", _FixedNow)


def _force_send_state(eng: DecisionEngine, now: datetime):
    """逃生阀确定性触发配方（与 test_daemon_fixes.test_bug1 一致）：
    高焦虑 ≥ block_th + 墙钟沉默 ≥72h + 冷却期外 → evaluate_triggers 必返
    Trigger("longing", escape_valve)；escape_valve_sleep_block 抬高到 >1 防
    Bayesian sleeping 高置信门控干扰。"""
    eng.netease_service.enabled = False  # 听歌反证与本修复无关，跳过网络
    eng.config.setdefault("bayesian", {})
    eng.config["bayesian"]["escape_valve_sleep_block"] = 1.01
    s = eng.state
    s.emotion.anxiety = 90.0
    s.emotion.loneliness = 90.0
    s.emotion.energy = 85.0
    s.cooldown.last_message_at = (now - timedelta(hours=80)).isoformat()
    s.cooldown.last_user_message_at = (now - timedelta(hours=80)).isoformat()
    s.cooldown.last_longing_break_at = None
    s.cooldown.messages_today = 0
    s.cooldown.messages_without_reply = 0
    s.cooldown.busy_until = None
    s.cooldown.event_timestamps = []
    s.cooldown.trigger_history = []


def _last_decision(eng: DecisionEngine):
    """读决策日志最后一行（无日志返回 None）。"""
    p = Path(eng.log_path)
    if not p.exists():
        return None
    lines = [ln for ln in p.read_text().splitlines() if ln.strip()]
    if not lines:
        return None
    return json.loads(lines[-1])


# ═══════════════════════════════════════════════════════
# R15 核心：save 失败 → 阻断 send
# ═══════════════════════════════════════════════════════

def test_save_fail_blocks_send_decision(tmp_path, capsys):
    """save() 返回 False 时 evaluate() 不得返回 send 决策（转 idle(state_save_failed)）。

    修复前红：save 失败仅 warn，evaluate 仍返回 action="send" →
    tick.sh `[ "$ACTION" = "send" ] || exit 0` 走发送链 → 状态未落盘 →
    下 tick 重新触发。断言 action != "send" 在修复前失败。
    """
    base = Path(tmp_path)
    eng = make_engine(base)
    now = dt(2026, 6, 15, 14, 0)  # 周一白天，静默窗口(0-8)外
    _force_send_state(eng, now)

    # 注入 save 故障（锁降级放弃写 / tmp 校验失败 / OSError 的统一返回值 False）
    with _fixed_now(now):
        with mock.patch.object(eng.state, "save", return_value=False) as m_save:
            d = eng.evaluate()

    # ① 核心断言：save 失败时不得输出 send 决策
    assert d.get("action") != "send", (
        f"save 失败必须阻断 send 决策（F-A18-04），实际 action={d.get('action')!r}: {d}")
    assert d.get("action") == "idle" and d.get("reason") == "state_save_failed", (
        f"save 失败应转 idle(reason=state_save_failed)，实际 {d}")

    # ② save 必须只被调用一次（send 阻断路径不得在 _emit_idle 内二次 save——
    # 否则瞬时故障下第二次 save 可能意外成功，把内存中未落盘的触发标记"部分落盘"
    # 而决策却是 idle → 消息静默丢失，比重复消息更坏；自审 #334 建议固化的锚点）
    m_save.assert_called_once()

    # ③ 可观测（stderr + audit 双层）：stderr 有明确告警，audit 落 state_save_failed
    err = capsys.readouterr().err
    assert "state_save_failed" in err, (
        f"save 失败路径必须有明确 stderr 告警，实际 stderr={err!r}")
    audit_path = Path(base) / "chiguo_state_audit.jsonl"
    assert audit_path.exists(), f"audit 文件应存在: {audit_path}"
    events = [json.loads(ln)["event"]
              for ln in audit_path.read_text().splitlines() if ln.strip()]
    assert "state_save_failed" in events, (
        f"audit 必须记录 state_save_failed 事件，实际 events={events}")

    # ④ send 决策不得落决策日志（避免下游消费到已记账但未落盘的 send）
    last = _last_decision(eng)
    assert last is None or last.get("action") != "send", (
        f"save 失败时决策日志不得出现 send 条目，实际最后一条={last}")


def test_save_ok_send_decision_unchanged(tmp_path):
    """对照：save 成功时行为与现状完全一致——仍返回 send 决策（正常路径零变化）。"""
    base = Path(tmp_path)
    eng = make_engine(base)
    now = dt(2026, 6, 15, 14, 0)
    _force_send_state(eng, now)

    with _fixed_now(now):
        d = eng.evaluate()

    assert d.get("action") == "send", (
        f"正常路径必须保持 send 决策，实际 action={d.get('action')!r}: {d}")
    assert d.get("msg_id") and d.get("trigger") and d.get("context"), \
        "send 决策字段完整（msg_id/trigger/context）"


# ═══════════════════════════════════════════════════════
# M5 修正：chiguo_state.py tmp 校验失败路径可观测
# ═══════════════════════════════════════════════════════

def test_tmp_validation_fail_warns(tmp_path, capsys):
    """tmp 校验失败路径（atomic_write verify 失败）返回 False 且必须有 stderr 告警。

    修复前红：该路径（chiguo_state.py:610-611）连 warn 都无——完全静默，
    审计 M5 修正要求补明确告警。
    """
    from chiguo_state import ChiguoState

    base = Path(tmp_path)
    cfg = {"_base_dir": str(base), "emotion": {},
           "memory": {"manual_path": str(base / "m.json")}}
    st = ChiguoState(cfg)
    st.emotion.loneliness = 50.0
    assert st.save(), "初始 save 失败（前置条件）"

    with mock.patch("chiguo_state.atomic_write",
                    side_effect=ValueError("tmp validation failed: unreadable")):
        ok = st.save()
    assert ok is False, "tmp 校验失败时 save 必须返回 False（不替换好状态）"

    err = capsys.readouterr().err
    assert "save skipped" in err and "tmp" in err, (
        f"tmp 校验失败路径必须补明确 stderr 告警（M5），实际 stderr={err!r}")