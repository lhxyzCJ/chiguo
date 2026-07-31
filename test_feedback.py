#!/usr/bin/env python3
"""test_feedback.py — v6 反馈闭环 单元测试"""

import json
import os
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

CST = timezone(timedelta(hours=8))

from chiguo_daemon import DecisionEngine
from chiguo_monitor import ChiguoMonitor


def _make_engine(temp_dir: str) -> DecisionEngine:
    """构造临时目录中的 DecisionEngine（不碰真实 state/log 文件）。"""
    td = Path(temp_dir)
    # 复制 toml 到临时目录（_base_dir 锚定到此）
    src = Path("chiguo_proactive.toml").read_text()
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
        assert r["refunded"] is False
    print("  OK test_record_send_result_success")


def test_record_send_result_no_error_success():
    """success 时不传 error → error 为 None"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        st.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        st.on_character_message(datetime.now(CST), "morning")
        r = engine.record_send_result("test_msg_3", "success")
        assert r["status"] == "success"
        assert r["error"] is None
        assert r["refunded"] is False
    print("  OK test_record_send_result_no_error_success")


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


def test_monitor_summary_includes_send_result():
    """summary() 人类可读文本含发送结果行"""
    with tempfile.TemporaryDirectory() as td:
        log_path = Path(td) / "decisions.jsonl"
        log_path.write_text(json.dumps({
            "action": "send_result", "msg_id": "a1", "status": "success",
            "time": "2026-07-31 10:00", "refunded": False,
            "state": {"emotion": {"loneliness": 50}, "cooldown": {}, "time": "2026-07-31 10:00"},
        }, ensure_ascii=False) + "\n")

        state_path = Path(td) / "state.json"
        state_path.write_text(json.dumps({"_version": 5, "last_tick": datetime.now(CST).isoformat()}))

        mon = ChiguoMonitor(str(log_path), str(state_path))
        text = mon.summary(days=7)
        assert "发送结果" in text, f"summary should include send result, got:\n{text}"
        assert "成功" in text
        assert "失败" in text
    print("  OK test_monitor_summary_includes_send_result")


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


if __name__ == "__main__":
    print("test_feedback.py\n")
    tests = [
        test_record_send_result_failed,
        test_record_send_result_success,
        test_record_send_result_no_error_success,
        test_monitor_stats_send_result,
        test_monitor_summary_includes_send_result,
        test_cli_send_result_branch,
        test_record_send_result_return_value,
        test_monitor_empty_send_result,
        test_record_send_result_idempotent,
        test_refund_then_can_send_after_min_interval,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"  FAIL {t.__name__}: {e}")
            import traceback
            traceback.print_exc()
            failed += 1

    print(f"\n{'='*40}")
    total = len(tests)
    passed = total - failed
    print(f"ALL {total} tests, {passed} passed, {failed} failed.")
    if failed:
        sys.exit(1)
    sys.exit(0)
