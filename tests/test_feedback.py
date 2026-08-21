#!/usr/bin/env python3
"""test_feedback.py — v6 反馈闭环 单元测试"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_daemon import DecisionEngine
from chiguo_monitor import ChiguoMonitor


def _make_engine(temp_dir: str) -> DecisionEngine:
    """构造临时目录中的 DecisionEngine（不碰真实 state/log 文件）。"""
    td = Path(temp_dir)
    # 复制 toml 到临时目录（_base_dir 锚定到此）
    import re
    src = Path("chiguo_proactive.toml").read_text()
    # 隔离:mem0_qdrant_path 改写为临时目录,防止新机器上连到生产记忆库
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{td / "no_qdrant"}"', src)
    cfg_path = td / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    log_path = td / "chiguo_decisions.jsonl"
    return DecisionEngine(str(cfg_path), str(log_path))


def test_record_send_result_failed():
    """failed → 退款：energy/anxiety 恢复，计数回滚，日志含 refunded=true"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now = datetime.now(CST)
        st = engine.state

        # 设置 current_date 防止第一次 send 被 _check_daily_reset 归零
        st.cooldown.current_date = now.strftime("%Y-%m-%d")

        # 记录发送前快照
        energy_before = st.emotion.energy
        anxiety_before = st.emotion.anxiety
        msgs_today_before = st.cooldown.messages_today
        mwr_before = st.cooldown.messages_without_reply
        events_len_before = len(st.cooldown.event_timestamps)

        # 模拟发送（引擎在 send 决策时调用 on_character_message）
        st.on_character_message(now, "lonely_mid")

        energy_after_send = st.emotion.energy
        anxiety_after_send = st.emotion.anxiety
        msgs_today_after_send = st.cooldown.messages_today
        mwr_after_send = st.cooldown.messages_without_reply
        events_len_after_send = len(st.cooldown.event_timestamps)

        assert energy_after_send < energy_before, "send should cost energy"
        assert msgs_today_after_send == msgs_today_before + 1
        assert mwr_after_send == mwr_before + 1
        assert events_len_after_send == events_len_before + 1

        # 退款
        engine.record_send_result("test_msg_1", "failed", "network timeout")

        # 验证退款
        assert st.emotion.energy == energy_before, \
            f"energy should be refunded: {st.emotion.energy} != {energy_before}"
        assert st.emotion.anxiety == anxiety_before, \
            f"anxiety should be refunded: {st.emotion.anxiety} != {anxiety_before}"
        assert st.cooldown.messages_today == msgs_today_before
        assert st.cooldown.messages_without_reply == mwr_before
        assert len(st.cooldown.event_timestamps) == events_len_before

        # 验证日志
        log_path = td + "/chiguo_decisions.jsonl"
        entries = [json.loads(l) for l in Path(log_path).read_text().strip().split("\n") if l]
        result_entries = [e for e in entries if e.get("action") == "send_result"]
        assert len(result_entries) == 1
        r = result_entries[0]
        assert r["msg_id"] == "test_msg_1"
        assert r["status"] == "failed"
        assert r["error"] == "network timeout"
        assert r["refunded"] is True
    print("  OK test_record_send_result_failed")


def test_record_send_result_success():
    """success → 不退款，日志含 refunded=false"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now = datetime.now(CST)
        st = engine.state
        st.cooldown.current_date = now.strftime("%Y-%m-%d")

        st.on_character_message(now, "lonely_mid")

        energy_after_send = st.emotion.energy
        anxiety_after_send = st.emotion.anxiety
        msgs_today = st.cooldown.messages_today
        mwr = st.cooldown.messages_without_reply
        events_len = len(st.cooldown.event_timestamps)

        engine.record_send_result("test_msg_2", "success")

        # 不应退款
        assert st.emotion.energy == energy_after_send, "success should not refund energy"
        assert st.emotion.anxiety == anxiety_after_send, "success should not refund anxiety"
        assert st.cooldown.messages_today == msgs_today
        assert st.cooldown.messages_without_reply == mwr
        assert len(st.cooldown.event_timestamps) == events_len

        # 验证日志
        log_path = td + "/chiguo_decisions.jsonl"
        entries = [json.loads(l) for l in Path(log_path).read_text().strip().split("\n") if l]
        result_entries = [e for e in entries if e.get("action") == "send_result"]
        assert len(result_entries) == 1
        r = result_entries[0]
        assert r["msg_id"] == "test_msg_2"
        assert r["status"] == "success"
        assert r["error"] is None
        assert r["refunded"] is False
    print("  OK test_record_send_result_success")


def test_monitor_stats_send_result():
    """ChiguoMonitor().stats() 含 send_success / send_failed 键"""
    with tempfile.TemporaryDirectory() as td:
        # 写日志到临时目录
        log_path = Path(td) / "decisions.jsonl"
        entries = [
            {"action": "send_result", "msg_id": "a1", "status": "success",
             "time": "2026-07-31 10:00", "refunded": False,
             "state": {"emotion": {"loneliness": 50}, "cooldown": {}, "time": "2026-07-31 10:00"}},
            {"action": "send_result", "msg_id": "a2", "status": "failed",
             "error": "timeout", "time": "2026-07-31 11:00", "refunded": True,
             "state": {"emotion": {"loneliness": 50}, "cooldown": {}, "time": "2026-07-31 11:00"}},
            {"action": "send_result", "msg_id": "a3", "status": "failed",
             "time": "2026-07-31 12:00", "refunded": True,
             "state": {"emotion": {"loneliness": 50}, "cooldown": {}, "time": "2026-07-31 12:00"}},
            {"action": "send_result", "msg_id": "a4", "status": "success",
             "time": "2026-07-31 13:00", "refunded": False,
             "state": {"emotion": {"loneliness": 50}, "cooldown": {}, "time": "2026-07-31 13:00"}},
        ]
        log_path.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries))

        state_path = Path(td) / "state.json"
        state_path.write_text(json.dumps({"_version": 5, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log_path), str(state_path))
        s = mon.stats(days=0)
        sr = s.get("send_result", {})
        assert sr["success"] == 2, f"expected 2 success, got {sr['success']}"
        assert sr["failed"] == 2, f"expected 2 failed, got {sr['failed']}"
    print("  OK test_monitor_stats_send_result")


def test_cli_send_result_branch():
    """验证 CLI --send-result 分支逻辑（直接调方法模拟 main 分支效果）"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now = datetime.now(CST)
        st = engine.state
        st.cooldown.current_date = now.strftime("%Y-%m-%d")

        energy_before = st.emotion.energy
        st.on_character_message(now, "lonely_mid")

        # 模拟 CLI: failed + error
        r = engine.record_send_result("cli_test_1", "failed", "rate limit")
        assert r["status"] == "failed"
        assert r["refunded"] is True
        assert r["error"] == "rate limit"
        assert st.emotion.energy == energy_before  # 已退款

        # 模拟 CLI: success（无error）
        st.on_character_message(now, "lonely_mid")
        r2 = engine.record_send_result("cli_test_2", "success")
        assert r2["status"] == "success"
        assert r2["refunded"] is False
    print("  OK test_cli_send_result_branch")


def test_record_send_result_return_value():
    """record_send_result 返回完整结果字典"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        st.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        st.on_character_message(datetime.now(CST), "lonely_mid")

        r = engine.record_send_result("ret_1", "failed", "test error")
        assert r["action"] == "send_result"
        assert r["msg_id"] == "ret_1"
        assert r["status"] == "failed"
        assert r["error"] == "test error"
        assert r["refunded"] is True
        assert "time" in r
    print("  OK test_record_send_result_return_value")


def test_record_send_result_idempotent():
    """同一 msg_id 重复上报 failed → 第二次不退款（v6 审计 MEDIUM 修复）"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        now = datetime.now(CST)
        st = engine.state
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        st.emotion.energy = 50.0
        st.on_character_message(now, "lonely_mid")
        r1 = engine.record_send_result("dup_1", "failed")
        assert r1["refunded"] is True
        e_after_first = st.emotion.energy
        r2 = engine.record_send_result("dup_1", "failed")
        assert r2["refunded"] is False, "second report must not refund"
        assert r2["duplicate"] is True
        assert st.emotion.energy == e_after_first, "no double refund"
    print("  OK test_record_send_result_idempotent")


def test_refund_then_can_send_after_min_interval():
    """退款后过 min_interval 可重发（逃生阀场景，审计补充）"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        now = datetime.now(CST)
        st = engine.state
        st.emotion.anxiety = 100.0  # 阻塞态
        st.cooldown.last_user_message_at = None  # silent 999h → 逃生阀激活
        st.cooldown.messages_today = 5  # 超限
        st.cooldown.last_message_at = now.isoformat()  # 刚发过 → min_interval 拦截
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        st.emotion.energy = 80.0
        assert st.can_send(now) is False, "min_interval should block immediately"
        engine.record_send_result("retry_1", "failed")
        later = now + timedelta(minutes=40)
        assert st.can_send(later) is True, "after refund + min_interval, retry allowed"
    print("  OK test_refund_then_can_send_after_min_interval")


def test_monitor_empty_send_result():
    """无 send_result 条目时 send_success/send_failed 均为 0"""
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "decisions.jsonl"
        log_path.write_text("")  # 空日志
        state_path = Path(td) / "state.json"
        state_path.write_text(json.dumps({"_version": 5, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log_path), str(state_path))
        s = mon.stats(days=0)
        sr = s.get("send_result", {})
        assert sr["success"] == 0
        assert sr["failed"] == 0
    print("  OK test_monitor_empty_send_result")


def test_recv_dedup_same_text_skipped():
    """v9/B2: 仅"无分析→带分析"升级副本去重；用户真实重发同文本（均无分析）→ 第二条完整处理"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        st.cooldown.last_message_at = now.isoformat()  # 使首次记录产生 reply_latency

        engine.record_user_message("哥哥在吗")
        e1 = st.emotion.energy
        a1 = st.emotion.affection
        lat1 = len(st.cooldown.reply_latencies)

        engine.record_user_message("哥哥在吗")  # 真实重发（无分析）→ 完整处理
        assert st.emotion.affection > a1, "真实重发应二次加好感"
        assert len(st.cooldown.reply_latencies) > lat1, "真实重发应二次追加延迟"
        assert st.cooldown.messages_without_reply == 0, "收到消息即清零（重复亦然）"

        # 去重标记持久化：重新加载状态后仍在
        engine2 = DecisionEngine(engine.config_path, engine.log_path)
        d = engine2.state.cooldown.recv_dedup
        assert d and d.get("analysis") is False and d.get("text_sha"), f"recv_dedup 未持久化: {d}"
    print("  OK test_recv_dedup_same_text_skipped")


def test_analysis_string_values_sanitized():
    """LLM 输出字符串数值（warmth="1.0"）→ 强转不崩溃，正常应用。
    同文本重复上报（已带分析）按新去重语义视为用户真实重发 → 走完整处理。"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")

        # 不同文本 → 每次都走完整处理路径（recv_dedup 只对同文本副本去重，避免短路假阳性）
        st.emotion.energy = 40.0  # 留出上限空间：第一条带分析 warmth=1.0 会显著提升 energy
        engine.record_user_message("哥哥在吗", '{"warmth": "1.0", "effort": "1.0", "attention": "1.0"}')
        e0 = st.emotion.energy
        engine.record_user_message("哥哥在忙吗", '{"warmth": "bad", "effort": null}')
        # 坏值回退默认 0 → 分析维度加成为 0；变化仅来自基础回复效果（A10 阻尼 ×0.5 → +5）
        assert abs((st.emotion.energy - e0) - 5.0) < 1e-6, \
            f"坏值应回退默认, energy 变化 {st.emotion.energy - e0}"
        # suppress_hours 字符串 → 强转 2 小时（断言具体值 ≈ now+2h，而非 truthy）
        engine.record_user_message("哥哥睡了吗", '{"suppress_hours": "2"}')
        until = st.cooldown.busy_suppress_until
        assert until, "suppress_hours 字符串应生效"
        delta = datetime.fromisoformat(until) - now
        assert timedelta(hours=1.9) < delta <= timedelta(hours=2.1), \
            f"suppress 应≈2h, got {delta}"
    print("  OK test_analysis_string_values_sanitized")


def test_recv_dedup_analysis_upgrade():
    """v9: bridge 先记录（无分析），standing order 补 --analysis → 只叠加分析微调，不重复基础效果"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")

        # U4 (#232, M-1): 状态永不越界（clamp 铁律）——初始 energy 调离上限，
        # 使升级 +8 后不触顶被截断（否则 delta 断言依赖越界落盘行为）
        st.emotion.energy = 50.0
        engine.record_user_message("哥哥在吗")
        e0, aff0 = st.emotion.energy, st.emotion.affection
        lat0 = len(st.cooldown.reply_latencies)

        engine.record_user_message("哥哥在吗", '{"warmth": 1.0, "effort": 1.0, "attention": 1.0}')
        # 升级只应用分析维度：energy += warmth*4 + attention*4；affection += warmth*1.5 + effort*1.0
        assert abs(st.emotion.energy - (e0 + 8.0)) < 1e-6, f"upgrade energy 异常: {st.emotion.energy - e0}"
        assert abs(st.emotion.affection - (aff0 + 2.5)) < 1e-6, f"upgrade affection 异常: {st.emotion.affection - aff0}"
        assert len(st.cooldown.reply_latencies) == lat0, "upgrade 不应追加回复延迟"
        assert st.cooldown.recv_dedup.get("analysis") is True

        # 日志含 recv_upgrade
        log = Path(td) / "chiguo_decisions.jsonl"
        kinds = [json.loads(l)["action"] for l in log.read_text().strip().splitlines()]
        assert "recv_upgrade" in kinds, kinds
    print("  OK test_recv_dedup_analysis_upgrade")


def test_recv_dedup_different_text_full_record():
    """v9: 窗口内不同文本 → 正常完整记录（不误杀真实新消息）；A10: 同窗口第 2 次回复加成 ×0.5"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")

        engine.record_user_message("哥哥在吗")
        st.emotion.energy = 50.0  # 留出 +10 空间（能量有 100 上限）
        # v1.14 (#139): record_user_message 锁内重载磁盘最新状态（防覆盖 evaluate
        # 落盘推进）→ 内存改动必须先落盘，否则会被重载回滚。落盘后 drop_events
        # 一并持久化（上一条同窗口回复事件），第二跳的 damp=0.5 语义不受影响。
        assert st.save()
        e1 = st.emotion.energy
        engine.record_user_message("哥哥在忙吗")  # 不同文本 → 完整效果
        # A10: 两条消息在同一 30 分钟阻尼窗口内 → 第 2 次同向加成 ×0.5 → energy +5（而非 +10）
        assert st.emotion.energy - e1 > 4.9, "不同文本应再次完整记录（阻尼后仍 +5）"
        assert st.emotion.energy - e1 < 5.1, f"damp=0.5 应给 +5, got {st.emotion.energy - e1}"
    print("  OK test_recv_dedup_different_text_full_record")


def test_phantom_send_reply_path_refund_and_monitor():
    """v1.11+R2: --user-msg 命中 send → 幻影退款（能量/messages_today/messages_without_reply/
    Hawkes 事件数恢复），send_result 日志带 error='phantom_send_reply_path'，且
    ChiguoMonitor 的 send_failed 统计不计该幻影记录（NTH-3 回归）。"""
    import decision.core as dcore
    from chiguo_trigger import Trigger
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        engine.netease_service.enabled = False  # 听歌反证与本用例无关，跳过网络
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        # 消除时段敏感：quiet 窗口置空 + 伪造非 sleeping 用户态（仿 test_escape_valve）
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        st.cooldown.last_message_at = None  # 从未发过 → min_interval 放行
        st.cooldown.messages_today = 0
        st.cooldown.messages_without_reply = 0
        st.cooldown.event_timestamps = []
        st.cooldown.trigger_history = []
        st.emotion.energy = 80.0
        st.infer_user_state = lambda now=None, msg_length=None: {
            "posterior": {"sleeping": 0.1, "browsing": 0.8, "busy": 0.1},
            "most_likely": "browsing", "confidence": 0.3, "utility": 0.1,
            "should_send_bayesian": True, "state_description": "browsing",
        }

        # ── 构造 --user-msg（无分析）分支：record_user_message → evaluate ──
        engine.record_user_message("哥哥在吗")
        energy_before = st.emotion.energy
        msgs_today_before = st.cooldown.messages_today
        mwr_before = st.cooldown.messages_without_reply
        events_before = len(st.cooldown.event_timestamps)

        # 使 evaluate 命中 send：替换触发评估为确定性 lonely_mid（回复链确有该路径——
        # 幻影记账正是由它造成；真实触发在情绪骤降后几乎不落 send，故测试用桩注入）
        decision = None
        orig = dcore.evaluate_triggers
        dcore.evaluate_triggers = lambda state, now, trigger_scale=None: \
            Trigger("lonely_mid", "medium")
        try:
            decision = engine.evaluate()
        finally:
            dcore.evaluate_triggers = orig

        assert decision["action"] == "send", f"expect send, got {decision.get('action')!r}"
        msg_id = decision.get("msg_id", "")

        # 发送记账已发生（能量消耗 / 日计数+1 / 未回复+1 / Hawkes+1）
        assert st.emotion.energy < energy_before, "send should cost energy"
        assert st.cooldown.messages_today == msgs_today_before + 1
        assert st.cooldown.messages_without_reply == mwr_before + 1
        assert len(st.cooldown.event_timestamps) == events_before + 1

        # ── CLI --user-msg 幻影退款分支（与 main() 相同调用）──
        engine.record_send_result(msg_id, "failed", error="phantom_send_reply_path")
        # 容差 1e-3：evaluate 内 _tick 会按 record_user_message 与 evaluate 间亚毫秒 elapsed
        # 微量推进情绪（~1e-7），退款只还原 20 能量成本，非精确相等。
        assert abs(st.emotion.energy - energy_before) < 1e-3, "phantom refund should restore energy"
        assert st.cooldown.messages_today == msgs_today_before
        assert st.cooldown.messages_without_reply == mwr_before
        assert len(st.cooldown.event_timestamps) == events_before

        # send_result 日志带 error=phantom_send_reply_path
        log_path = td + "/chiguo_decisions.jsonl"
        entries = [json.loads(l) for l in Path(log_path).read_text().strip().split("\n") if l]
        phantom = [e for e in entries if e.get("action") == "send_result"]
        assert len(phantom) == 1, f"expect 1 send_result, got {len(phantom)}"
        assert phantom[0]["status"] == "failed"
        assert phantom[0]["error"] == "phantom_send_reply_path"
        assert phantom[0]["refunded"] is True

        # monitor send_failed 不计幻影记录（NTH-3）
        state_path = td + "/chiguo_state.json"
        mon = ChiguoMonitor(log_path, state_path)
        s = mon.stats(days=0)
        sr = s.get("send_result", {})
        assert sr["failed"] == 0, f"phantom refund must not count as send_failed, got {sr}"
    print("  OK test_phantom_send_reply_path_refund_and_monitor")
