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


# ═══════════════════════════════════════════════════════
# F-A15-002: refunded msg_id 有界 FIFO —— 同 msg_id 越窗口重放的双退拒收。
# ═══════════════════════════════════════════════════════

def test_refund_same_msg_id_dedup_fifo():
    """msg_id 首次退款成功 → 记入 refunded FIFO；第二次同 msg_id 退款被拒（不透支退款副作用）。

    修复前红：refund_send 无 refunded_msg_ids 记账 → 帧内重新构造同 msg_id 的
    in-flight 事件重放退款，第二次仍全额退款（重放双退）；修复后第二次返回 False。"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = _seed(st, events=[{"msg_id": "dup1", "time": "t1"}, {"type": "lonely_mid", "time": "t2"}])
        # 首次退款：匹配 dup1 → True，且 refunded_msg_ids 记录 dup1
        assert st.refund_send(now, msg_id="dup1") is True, "首次匹配退款应成功"
        assert "dup1" in st.cooldown.refunded_msg_ids, \
            f"首次退款后应把 msg_id 记录进 refunded FIFO: {st.cooldown.refunded_msg_ids}"
        # 同 msg_id 第二次退款（即便 again 有匹配 in-flight 事件）→ 必须被 FIFO 拒收
        again_seed = _seed(st, events=[{"msg_id": "dup1", "time": "t1b"}, {"type": "lonely_mid", "time": "t3"}])
        assert st.refund_send(again_seed, msg_id="dup1") is False, \
            "同一 msg_id 第二次退款应被 FIFO 拒收（防越窗口双退）"


def test_uncertain_clears_unreplied_only_dedup():
    """RF11 (M2): timeout_uncertain 的**轻量清算**——只回滚 messages_without_reply，
    不清 energy/quota/逃生阀冷却/Hawkes（防已送达时制造重发窗口）；经 record_send_result
    ('uncertain') 落盘且同 msg_id 幂等（_has_send_result 去重，二次不重复清）。
    修复前红：timeout_uncertain 无清算通道 → 未回复计数无限累积致 silent。"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = _seed(st, events=[{"msg_id": "unc1", "time": "t1"}, {"type": "lonely_mid", "time": "t2"}])
        mwr_before = st.cooldown.messages_without_reply  # _seed 置 2
        energy_before = st.emotion.energy                 # 50
        anxious_before = st.emotion.anxiety               # 30
        escape_before = st.cooldown.last_longing_break_at  # now.isoformat() 非 None
        events_before = list(st.cooldown.event_timestamps)  # 2 条，不应被删

        # ① 直接方法层：只清未回复计数，其余副作用不动
        st.clear_unreplied(now)
        assert st.cooldown.messages_without_reply == mwr_before - 1, \
            f"clear_unreplied 应只 -1 未回复计数: {st.cooldown.messages_without_reply}"
        assert st.emotion.energy == energy_before, "clear_unreplied 不应回滚 energy"
        assert st.emotion.anxiety == anxious_before, "clear_unreplied 不应回滚 anxiety"
        assert st.cooldown.last_longing_break_at == escape_before, \
            "clear_unreplied 不应重置逃生阀冷却"
        assert st.cooldown.event_timestamps == events_before, \
            "clear_unreplied 不应删除 Hawkes 事件"
        # ② 下限夹紧：不降到负
        st.cooldown.messages_without_reply = 0
        st.clear_unreplied(now)
        assert st.cooldown.messages_without_reply == 0, "clear_unreplied 应 clamp 到 0"
        # ③ 经 daemon record_send_result('uncertain') 落盘 + 幂等
        st.cooldown.messages_without_reply = 3
        r1 = engine.record_send_result("unc1", "uncertain", "timeout_uncertain")
        assert r1["status"] == "uncertain" and r1["refunded"] is True, r1
        # 落盘后 on-disk 未回复计数 = 2（3→2）
        on_disk = json.loads(Path(td, "chiguo_state.json").read_text())
        assert on_disk["cooldown"]["messages_without_reply"] == 2, \
            f"uncertain 清算应落盘: {on_disk['cooldown']}"
        # 同 msg_id 二次 uncertain → _has_send_result 去重，不再重复清（refunded False）
        r2 = engine.record_send_result("unc1", "uncertain", "timeout_uncertain")
        assert r2["duplicate"] is True, f"同 msg_id 二次 uncertain 应被去重: {r2}"
        assert engine.state.cooldown.messages_without_reply == 2, \
            "二次 uncertain 不应重复清未回复计数"


# ═══════════════════════════════════════════════════════════
# F-A5-01（#314 R9）: 三机制③「决策即标记、refund 不回滚」→ 发送失败后
# reminder 永久不再触发。修复：refund_send 按 msg_id 定位到在途事件携带的
# memory_marker（决策核心提交时写入），回滚 last_triggered_at → 失败可重发。
#═══════════════════════════════════════════════════════════════

def test_refund_rolls_back_reminder_marker():
    """F-A5-01 ③：send 决策标记 reminder → refund → last_triggered_at 被回滚，
    reminder 可再次触发（_memory_should_trigger 恢复 True）。

    决策核心（decision/core.py）在标记 reminder 的同时，把该记忆的内容键写入
    在途 Hawkes 事件的 memory_marker 字段（跨进程随 cooldown 落盘）。refund_send
    删除命中事件后据此清掉 last_triggered_at——否则失败即永久丢提醒。"""
    from chiguo_state import _memory_dedup_key
    from chiguo_trigger import _memory_should_trigger
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)  # 固定时钟：触发时刻 5min 前 → 30min 窗内
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        # 临时目录写一条 reminder 记忆（触发时刻 5 分钟前 → 窗口内）
        (Path(td) / "data").mkdir(exist_ok=True)
        (Path(td) / "data" / "chiguo_memories.json").write_text(json.dumps([
            {"type": "reminder", "trigger_at": "2026-06-15T13:55", "content": "喝水"}
        ]))
        # 新实例加载记忆（决策核心同款：self.memories 持有该 dict）
        st2 = _make_engine(td).state
        rem = next(m for m in st2.memories if m.get("type") == "reminder")
        # 决策核心标记 + 记忆键写入在途事件（Commit 4 写入契约）
        st2.mark_memory_triggered(rem, now)
        mid = "reminder_send_1"
        st2.cooldown.event_timestamps = [
            {"msg_id": mid, "type": "memory", "time": now.isoformat(),
             "memory_marker": _memory_dedup_key(rem)},
        ]
        st2.cooldown.current_date = now.strftime("%Y-%m-%d")
        assert rem.get("last_triggered_at") == now.isoformat(), \
            f"决策标记应生效, got {rem}"
        assert _memory_should_trigger(rem, now) is False, \
            "标记后窗口内不得重复触发"

        # 发送失败 → refund 回滚标记
        assert st2.refund_send(now, msg_id=mid) is True, "命中事件应退款成功"
        # 回滚断言：last_triggered_at 被清除 + 去重缓存清除 + 可再次触发
        assert "last_triggered_at" not in rem, \
            f"refund 应清除 last_triggered_at, got {rem}"
        assert _memory_dedup_key(rem) not in st2._memory_dedup, \
            f"refund 应清除去重缓存, got {st2._memory_dedup}"
        assert _memory_should_trigger(rem, now) is True, \
            "refund 后 reminder 窗口内应可再次触发"
    print("  OK test_refund_rolls_back_reminder_marker")


def test_refund_rollback_survives_state_roundtrip():
    """F-A5-01 ③（跨进程）：refund 回滚后经 state roundtrip，last_triggered_at
    不回弹、memory_dedup 不含该条目 → cron 下一进程可再次触发。"""
    from chiguo_state import _memory_dedup_key
    from chiguo_trigger import _memory_should_trigger
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)  # 固定时钟：窗口内（触发前 5min）
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "data").mkdir(exist_ok=True)
        (Path(td) / "data" / "chiguo_memories.json").write_text(json.dumps([
            {"type": "reminder", "trigger_at": "2026-06-15T13:55", "content": "喝水"}
        ]))
        eng = _make_engine(td)
        st = eng.state
        rem = next(m for m in st.memories if m.get("type") == "reminder")
        st.mark_memory_triggered(rem, now)
        mid = "reminder_cross1"
        st.cooldown.event_timestamps = [
            {"msg_id": mid, "type": "memory", "time": now.isoformat(),
             "memory_marker": _memory_dedup_key(rem)},
        ]
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        assert st.save(), "前置 save"
        disk_before = json.loads(st.state_path.read_text())
        assert disk_before["memory_dedup"], "标记应落盘 memory_dedup"

        # 退款并落盘
        assert st.refund_send(now, msg_id=mid) is True
        assert st.save(), "退款后 save"

        # 新实例（等价新进程）加载：标记不回弹，可再次触发
        st2 = _make_engine(td).state
        rem2 = next(m for m in st2.memories if m.get("type") == "reminder")
        disk_after = json.loads(st2.state_path.read_text())
        assert "memory_dedup" not in disk_after or \
            disk_after["memory_dedup"].get(_memory_dedup_key(rem2)) is None, \
            f"回滚后 memory_dedup 不得含该条目, got {disk_after.get('memory_dedup')}"
        assert "last_triggered_at" not in rem2, \
            f"回滚跨进程不应回弹 last_triggered_at, got {rem2}"
        assert _memory_should_trigger(rem2, now) is True, \
            "refund 落盘后新进程应可再次触发"
    print("  OK test_refund_rollback_survives_state_roundtrip")





def test_refund_fifo_is_bounded():
    """refunded_msg_ids FIFO 有界：超过上限后只保留最近 N 条（不无限增长）。"""
    from chiguo_state import REFUND_FIFO_MAX
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        # 无上限直接爆量会导致超大 state.json；有界即让 FIFO 被裁到 ≤ REFUND_FIFO_MAX
        for i in range(REFUND_FIFO_MAX + 50):
            _seed(st, events=[{"msg_id": f"m{i}", "time": "t"}])
            assert st.refund_send(datetime.now(CST), msg_id=f"m{i}") is True
        assert len(st.cooldown.refunded_msg_ids) <= REFUND_FIFO_MAX, \
            f"refunded FIFO 应被限制在 ≤{REFUND_FIFO_MAX}: {len(st.cooldown.refunded_msg_ids)}"
        assert "m0" not in st.cooldown.refunded_msg_ids, "最旧条目应被挤出"
