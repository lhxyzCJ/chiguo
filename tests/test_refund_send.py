#!/usr/bin/env python3
"""test_refund_send.py — refund_send 语义矩阵（Q30 收敛契约固化，审计 #282 低严重度）

refund_send 已把 msg_id/legacy 判定收敛到单处（Q30），返回 True/False 决定
调用方（daemon record_send_result）是否 save 落盘。本文件用参数化 runner
直接锁定四种分支的返回值 + 副作用（成本回滚 / 事件删除 / 逃生阀冷却重置），
并经 daemon 联动验证「True → 落盘、False → 不落盘」的 save 语义。

四分支（msg_id 语义矩阵）：
  1. 空在途（事件为空）        → return False，不退款、不删、不重置冷却
  2. 未知 msg_id（modern 事件不匹配）→ return False，不退款、不删（现代事件保留）
  3. 匹配 msg_id（命中）       → return True，删除命中事件 + 回滚副作用
  4. 全 legacy（无 msg_id 旧事件）→ return True，pop 最后一条 + 回滚副作用
"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_daemon import DecisionEngine


def _make_engine(td: str) -> DecisionEngine:
    """临时目录构造 DecisionEngine（_base_dir 锚定到 td，隔离 mem0/state/log）。"""
    td = Path(td)
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{td / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{td / "no_history.db"}"', src)
    cfg_path = td / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    log_path = td / "chiguo_decisions.jsonl"
    return DecisionEngine(str(cfg_path), str(log_path))


def _seed(st, energy=50.0, anxiety=30.0, msgs_today=3, mwr=2, events=None):
    """放置确定基线并返回「should_apply/skip 时不可变」的初始量（供 4 分支共用）。

    current_date 固定为今天 → _finalize 的跨日重置不干扰（退款才会触发 daily
    reset 的归零，这里保证退款前后量纲一致）。
    """
    now = datetime.now(CST)
    st.cooldown.current_date = now.strftime("%Y-%m-%d")
    st.emotion.energy = energy
    st.emotion.anxiety = anxiety
    st.cooldown.messages_today = msgs_today
    st.cooldown.messages_without_reply = mwr
    st.cooldown.last_longing_break_at = now.isoformat()  # 非 None → True 分支应清空
    # 复制一份（refund_send 会 del/pop 原地改 list）——同一事件列表被多处 _seed
    # 传入时必须以副本隔离，防止前一分支的原地删除污染后续分支的基准。
    st.cooldown.event_timestamps = [dict(e) for e in (events or [])]
    return now


def _expect_refund_deltas(st):
    """从 toml 读退款副作用的期望增量（不硬编码默认值）。"""
    emo = st.config.get("emotion", {})
    cost = float(emo.get("energy_cost_per_message", 20.0))
    anx_gain = float(emo.get("anxiety_gain_on_send", 2.0))
    return cost, anx_gain


def _assert_unrefunded(st, before):
    """False 分支：无任何退款副作用，冷却不重置，事件列表不变。"""
    assert st.emotion.energy == before["energy"], (
        f"能量不得回滚: {st.emotion.energy} != {before['energy']}")
    assert st.emotion.anxiety == before["anxiety"], (
        f"不安不得回滚: {st.emotion.anxiety} != {before['anxiety']}")
    assert st.cooldown.messages_today == before["msgs_today"], "日计数不得回滚"
    assert st.cooldown.messages_without_reply == before["mwr"], "未回复计数不得回滚"
    assert st.cooldown.last_longing_break_at is not None, \
        "逃生阀冷却不得被未知 msg_id 凭空重置（#83）"
    assert st.cooldown.event_timestamps == before["events"], "事件列表必须原样保留"


def _assert_refunded(st, before, removed_events, expect_same_energy=None):
    """True 分支：成本回滚 + 冷却重置 + 事件被移除/弹出。"""
    cost, anx_gain = _expect_refund_deltas(st)
    assert st.emotion.energy == min(100.0, before["energy"] + cost), (
        f"能量应回滚 +{cost}: {st.emotion.energy} != {before['energy'] + cost}")
    assert st.emotion.anxiety == max(0.0, before["anxiety"] - anx_gain), (
        f"不安应回滚 -{anx_gain}: {st.emotion.anxiety} != {before['anxiety'] - anx_gain}")
    assert st.cooldown.messages_today == before["msgs_today"] - 1, "日计数应 -1"
    assert st.cooldown.messages_without_reply == before["mwr"] - 1, "未回复计数应 -1"
    assert st.cooldown.last_longing_break_at is None, "逃生阀冷却应清空（未送达不白扣）"
    assert st.cooldown.event_timestamps == removed_events, "事件移除结果不符"


# ═══════════════════════════════════════════════════════
# 参数化 runner：四种分支
# ═══════════════════════════════════════════════════════

def _refund_matrix_run(td, events, msg_id, expect_save: bool, removed_events,
                       events_after_daemon=None) -> None:
    """单分支执行：先直接调 refund_send 断言（返回 + 副作用），
    再经 daemon record_send_result 联动断言 save 语义（True→落盘 / False→不落盘）。"""
    engine = _make_engine(td)
    st = engine.state
    now = _seed(st, events=events)
    before = {
        "energy": st.emotion.energy,
        "anxiety": st.emotion.anxiety,
        "msgs_today": st.cooldown.messages_today,
        "mwr": st.cooldown.messages_without_reply,
        "events": list(st.cooldown.event_timestamps),
    }

    # ── ① 直接单元断言：refund_send 返回值 + 副作用 ──
    r = st.refund_send(now, msg_id=msg_id)
    assert r is expect_save, (
        f"branch msg_id={msg_id!r} events={events}: refund_send 应返回 "
        f"{expect_save}, got {r}")
    if expect_save:
        _assert_refunded(st, before, removed_events)
    else:
        _assert_unrefunded(st, before)

    # ── ② daemon 联动 save 语义：新 engine（磁盘原文快照）→ record_send_result ──
    # 落盘断言以独立 engine 从磁盘读回为准（避免与 ① 的内存副作用混淆）。
    da_engine = _make_engine(td)
    da_state = da_engine.state
    _seed(da_state, events=events)
    assert da_state.save(), "联动前置：初始状态需落盘"  # 防 record_send_result 锁内 _load 重置
    disk_before = json.loads(da_state.state_path.read_text())

    res = da_engine.record_send_result(msg_id, "failed", "linkage-test")
    assert res["refunded"] is expect_save, (
        f"record_send_result refunded 应等于 refund_send 判定 {expect_save}, "
        f"got {res['refunded']!r}")

    disk_after = json.loads(da_state.state_path.read_text())
    after_events = disk_after["cooldown"]["event_timestamps"]
    if expect_save:
        # True → refund_send 触发 save：事件移除必须已落盘
        assert after_events == removed_events, (
            f"True 分支 save 联动：落盘事件应为 {removed_events}, got {after_events}")
        assert disk_after["emotion"]["energy"] > disk_before["emotion"]["energy"], \
            "True 分支 save 联动：退款后的能量应落盘"
    else:
        # False → 不落盘：磁盘事件/能量原样（未知 msg_id 不得凭空产生副作用）
        assert after_events == disk_before["cooldown"]["event_timestamps"], (
            f"False 分支不应落盘副作用：事件从 {disk_before['cooldown']['event_timestamps']} "
            f"变成 {after_events}")
        assert disk_after["emotion"]["energy"] == disk_before["emotion"]["energy"], \
            "False 分支不应落盘能量回滚"
    print(f"  OK matrix: msg_id={msg_id!r} events={events} → refund_send={r} save={expect_save}")


def test_empty_inflight():
    # 分支 1：空在途（msg_id 非空但无事件）→ False，不退款不删
    with tempfile.TemporaryDirectory() as td:
        _refund_matrix_run(td, events=[], msg_id="never_sent",
                           expect_save=False, removed_events=[])


def test_unknown_msg_id():
    # 分支 2：未知 msg_id（modern 事件不匹配）→ False，不退款不删（现代事件保留）
    events = [{"msg_id": "m1", "time": "t1"}, {"msg_id": "m2", "time": "t2"}]
    with tempfile.TemporaryDirectory() as td:
        _refund_matrix_run(td, events=events, msg_id="mX",
                           expect_save=False, removed_events=events)


def test_matched_msg_id():
    # 分支 3：匹配 msg_id → 删除命中事件 + 回滚，返回 True
    events = [{"msg_id": "m1", "time": "t1"}, {"type": "lonely_mid", "time": "t2"}]
    removed = [{"type": "lonely_mid", "time": "t2"}]  # 仅 m1 被删除，其余保留
    with tempfile.TemporaryDirectory() as td:
        _refund_matrix_run(td, events=events, msg_id="m1",
                           expect_save=True, removed_events=removed)


def test_all_legacy():
    # 分支 4：全 legacy（无 msg_id 旧事件）→ pop 回滚，返回 True
    events = [{"type": "lonely_mid", "time": "t1"}, {"type": "anxiety", "time": "t2"}]
    removed = [{"type": "lonely_mid", "time": "t1"}]  # pop 最后一条
    with tempfile.TemporaryDirectory() as td:
        _refund_matrix_run(td, events=events, msg_id="m9",
                           expect_save=True, removed_events=removed)
