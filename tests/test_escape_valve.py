#!/usr/bin/env python3
"""test_escape_valve.py — 溢出逃生阀单元测试 v6"""

import sys, os
import re
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState, ChiguoEmotion, CooldownState
from chiguo_trigger import evaluate_triggers


def _base_cfg(tmp: str) -> dict:
    """最小化配置，所有文件路径指向临时目录，不依赖真实课表/记忆库"""
    return {
        "_base_dir": str(tmp),
        "emotion": {},
        "cooldown": {
            "anxiety_block_threshold": 70.0,
            "longing_break_enabled": True,
            "longing_break_min_silence_hours": 72,
            "longing_break_cooldown_days": 3,
            "max_daily_active": 4,
            "max_daily_silent": 2,
            "min_interval_minutes": 30,
        },
        "poisson": {"base_lambda": 0.25},
        "schedule": {"quiet_start": 0, "quiet_end": 8},
        "sigmoid": {},
    }


def _make_state(cfg: dict) -> ChiguoState:
    """构造干净的测试用 ChiguoState，不加载磁盘残留状态"""
    state = ChiguoState(cfg)
    emo_cfg = cfg.get("emotion", {})
    state.emotion = ChiguoEmotion(
        loneliness=emo_cfg.get("loneliness", 15.0),
        affection=emo_cfg.get("affection", 55.0),
        anxiety=emo_cfg.get("anxiety", 40.0),
        energy=emo_cfg.get("energy", 85.0),
    )
    state.cooldown = CooldownState()
    state.cooldown.current_date = str(datetime.now(CST).date())
    return state


# ═══════════════════════════════════════════════════════════
# longing_break_eligible 测试
# ═══════════════════════════════════════════════════════════

def test_deadlock_eligible_no_last_msg():
    """死锁态：焦虑≥阈值 + 从未收到过主人消息 → 999h沉默 → eligible"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        # last_user_message_at 为 None → silent_hours(wall=True) 返回 999.0（天然满足 72h）
        now = datetime.now(CST)
        assert state.longing_break_eligible(now), "deadlock (no msg ever) should be eligible"
        print("  OK test_deadlock_eligible_no_last_msg")


def test_deadlock_eligible_4_days_silence():
    """死锁态：焦虑≥阈值 + 4天前最后一条消息 → silent_hours(wall=True) ≈ 96h → eligible"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        assert state.longing_break_eligible(now), "deadlock (4 days silence) should be eligible"
        print("  OK test_deadlock_eligible_4_days_silence")


def test_non_blocked_not_eligible():
    """非阻塞态：焦虑 40 < 70 → 不走逃生阀，交给正常 overflow 路径"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 40.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        assert not state.longing_break_eligible(now), "anxiety=40 < threshold, not deadlock"
        print("  OK test_non_blocked_not_eligible")


def test_insufficient_silence():
    """沉默不足：焦虑=100 但最后消息仅 1 天前 → silent_hours(wall=True) ≈ 24h < 72h"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=1)).isoformat()
        assert not state.longing_break_eligible(now), "silence=24h < 72h min"
        print("  OK test_insufficient_silence")


def test_cooldown_active_1_day():
    """冷却中：on_longing_break 后仅过 1 天 → 冷却未过，不触发"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        # 1 天前触发过破防
        state.cooldown.last_longing_break_at = (now - timedelta(days=1)).isoformat()
        assert not state.longing_break_eligible(now), "1 day after break → cooling"
        print("  OK test_cooldown_active_1_day")


def test_cooldown_expired_3_days():
    """冷却已过：on_longing_break 后 3 天 → 冷却期满，可再次破防"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        state.cooldown.last_longing_break_at = (now - timedelta(days=3)).isoformat()
        assert state.longing_break_eligible(now), "3 days after break → cooldown expired"
        print("  OK test_cooldown_expired_3_days")


def test_disabled_via_config():
    """配置关闭：longing_break_enabled=false → 永不触发"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        cfg["cooldown"]["longing_break_enabled"] = False
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        assert not state.longing_break_eligible(now), "disabled in config"
        print("  OK test_disabled_via_config")


# ═══════════════════════════════════════════════════════════
# evaluate_triggers 逃生阀返回测试
# ═══════════════════════════════════════════════════════════

def test_evaluate_triggers_escape_valve():
    """eligible 时 evaluate_triggers 强制返回 longing+high+escape_valve"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        now = datetime.now(CST)
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()

        trigger = evaluate_triggers(state, now)
        assert trigger is not None, "escape valve should produce a trigger"
        assert trigger.type == "longing", f"expected longing, got {trigger.type}"
        assert trigger.intensity == "high", f"expected high, got {trigger.intensity}"
        assert trigger.data.get("escape_valve") is True, \
            f"expected escape_valve=True in data, got {trigger.data}"
        print("  OK test_evaluate_triggers_escape_valve")


def test_evaluate_triggers_no_escape_valve_when_not_eligible():
    """非 eligible 时 evaluate_triggers 不返回逃生阀（走正常候选收集）"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 40.0
        now = datetime.now(CST)
        # 低焦虑 + 刚收到消息 → 不 eligible
        state.cooldown.last_user_message_at = now.isoformat()

        trigger = evaluate_triggers(state, now)
        # 逃生阀不应触发（trigger 可能是其他类型或 None，但不能是 escape_valve）
        if trigger is not None:
            assert not trigger.data.get("escape_valve"), \
                f"escape valve should not fire when not eligible, got {trigger.data}"
        print("  OK test_evaluate_triggers_no_escape_valve_when_not_eligible")


# ═══════════════════════════════════════════════════════════
# can_send 逃生阀突破日限额测试
# ═══════════════════════════════════════════════════════════

def test_can_send_escape_valve_over_daily_limit():
    """逃生阀 eligible 时，即使超过日限额也能发送"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 100.0
        state.emotion.energy = 85.0
        now = datetime.now(CST)
        # 4 天前最后一条消息 → 沉默 > 8h → daily_max_silent = 2
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        state.cooldown.current_date = now.strftime("%Y-%m-%d")
        state.cooldown.messages_today = 2  # 已达到沉默上限
        state.cooldown.last_message_at = None  # 无间隔限制
        state.cooldown.set_quiet_window(0, 0)  # 空窗口:消除 0-8 时段的时段敏感(本测试只验日限额放行)

        assert state.cooldown.messages_today >= cfg["cooldown"]["max_daily_silent"], \
            "precondition: messages_today should be at limit"
        assert state.longing_break_eligible(now), "precondition: should be eligible"
        assert state.can_send(now), "escape valve should bypass daily limit"
        print("  OK test_can_send_escape_valve_over_daily_limit")


def test_can_send_daily_limit_blocks_when_not_eligible():
    """非 eligible 时，日限额照常拦截"""
    with tempfile.TemporaryDirectory() as tmp:
        cfg = _base_cfg(tmp)
        state = _make_state(cfg)
        state.emotion.anxiety = 40.0  # 不阻塞
        state.emotion.energy = 85.0
        now = datetime.now(CST)
        # 4 天前消息 → 沉默 > 8h → daily_max_silent = 2
        state.cooldown.last_user_message_at = (now - timedelta(days=4)).isoformat()
        state.cooldown.current_date = now.strftime("%Y-%m-%d")
        state.cooldown.messages_today = 2
        state.cooldown.last_message_at = None
        state.cooldown.set_quiet_window(0, 0)  # 空窗口:消除 0-8 时段的时段敏感(本测试只验日限额拦截)

        assert state.cooldown.messages_today >= cfg["cooldown"]["max_daily_silent"], \
            "precondition: messages_today should be at limit"
        assert not state.longing_break_eligible(now), "precondition: not eligible"
        assert not state.can_send(now), "daily limit should block when not eligible"
        print("  OK test_can_send_daily_limit_blocks_when_not_eligible")


def _isolated_toml(cfg_path: Path, tmp: Path) -> None:
    """真实 toml 副本隔离:mem0_qdrant_path/history_db 改写为临时目录,防止新机器上连到生产记忆库"""
    txt = cfg_path.read_text()
    txt = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{tmp / "no_qdrant"}"', txt)
    txt = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{tmp / "no_history.db"}"', txt)
    cfg_path.write_text(txt)


def test_end_to_end_escape_valve_send():
    """端到端: 死锁态 → evaluate() 出 send 决策, trigger=longing, context 含【破防】"""
    from chiguo_daemon import DecisionEngine
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg_path = td_path / "chiguo_proactive.toml"
        cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
        _isolated_toml(cfg_path, td_path)
        engine = DecisionEngine(str(cfg_path), str(td_path / "decisions.jsonl"))
        # 消除时段敏感：quiet 窗口设为空
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        st = engine.state
        st.emotion.anxiety = 100.0  # 阻塞态
        # 4 天前交互 → 沉默 >72h 死锁态（v7: 从未交互不触发逃生阀，故不用 None）
        st.cooldown.last_user_message_at = (datetime.now(CST) - timedelta(days=4)).isoformat()
        st.cooldown.messages_today = 5  # 超日限额 → 逃生阀放行
        st.cooldown.last_message_at = (datetime.now(CST) - timedelta(hours=2)).isoformat()
        st.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        st.cooldown.event_timestamps = []
        # 消除时段敏感:伪造非 sleeping 用户态(真实 infer 在 00-08 点会报 sleeping 高置信 → sleeping_guard)
        st.infer_user_state = lambda now=None, msg_length=None: {
            "posterior": {"sleeping": 0.1, "browsing": 0.8, "busy": 0.1},
            "most_likely": "browsing", "confidence": 0.3, "utility": 0.1,
            "should_send_bayesian": True, "state_description": "browsing",
        }
        decision = engine.evaluate()
        assert decision["action"] == "send", \
            f"expected send, got {decision['action']}: {decision.get('reason')}"
        assert decision["trigger"] == "longing", decision.get("trigger")
        assert decision["intensity"] == "high"
        assert "【破防】" in decision["context"]["layer_guidance"], \
            "context should contain 破防 note"
        assert st.cooldown.last_longing_break_at is not None, \
            "cooldown timestamp should be recorded"
    print("  OK test_end_to_end_escape_valve_send")


def test_escape_valve_bypasses_bayesian_sleeping():
    """逃生阀激活时 Bayesian sleeping 覆盖不拦截（v6 审计 CRITICAL 修复）"""
    from chiguo_daemon import DecisionEngine
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg_path = td_path / "chiguo_proactive.toml"
        cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
        _isolated_toml(cfg_path, td_path)
        engine = DecisionEngine(str(cfg_path), str(td_path / "decisions.jsonl"))
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        st = engine.state
        st.emotion.anxiety = 100.0
        # 4 天前交互 → 沉默 >72h 死锁态（v7: 从未交互不触发逃生阀，故不用 None）
        st.cooldown.last_user_message_at = (datetime.now(CST) - timedelta(days=4)).isoformat()
        st.cooldown.messages_today = 5
        st.cooldown.last_message_at = (datetime.now(CST) - timedelta(hours=2)).isoformat()
        st.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        st.cooldown.event_timestamps = []
        # 伪造 Bayesian sleeping 高置信（真实 infer 在 999h 沉默下返回 browsing）
        st.infer_user_state = lambda now=None, msg_length=None: {
            "posterior": {"sleeping": 0.9, "browsing": 0.05, "busy": 0.05},
            "most_likely": "sleeping", "confidence": 0.8, "utility": 0.1,
            "should_send_bayesian": False, "state_description": "sleeping",
        }
        decision = engine.evaluate()
        assert decision["action"] == "send", \
            f"escape valve should bypass Bayesian sleeping, got {decision['action']}: {decision.get('reason')}"
        assert decision["trigger"] == "longing"
    print("  OK test_escape_valve_bypasses_bayesian_sleeping")


def test_sleeping_guard_blocks_escape_valve_at_high_confidence():
    """v7: 逃生阀激活 + sleeping 置信度 ≥ escape_valve_sleep_block(0.9) → 降级 idle(sleeping_guard)"""
    from chiguo_daemon import DecisionEngine
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg_path = td_path / "chiguo_proactive.toml"
        cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
        _isolated_toml(cfg_path, td_path)
        engine = DecisionEngine(str(cfg_path), str(td_path / "decisions.jsonl"))
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        st = engine.state
        st.emotion.anxiety = 100.0
        st.cooldown.last_user_message_at = (datetime.now(CST) - timedelta(days=4)).isoformat()
        st.cooldown.messages_today = 5
        st.cooldown.last_message_at = (datetime.now(CST) - timedelta(hours=2)).isoformat()
        st.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        st.cooldown.event_timestamps = []
        # 伪造 Bayesian sleeping 置信度 0.95 ≥ escape_valve_sleep_block（默认 0.9，用配置默认值）
        st.infer_user_state = lambda now=None, msg_length=None: {
            "posterior": {"sleeping": 0.95, "browsing": 0.03, "busy": 0.02},
            "most_likely": "sleeping", "confidence": 0.95, "utility": 0.1,
            "should_send_bayesian": False, "state_description": "sleeping",
        }
        st.cooldown.held_count = 7
        decision = engine.evaluate()
        assert decision["action"] == "idle", \
            f"sleeping_guard should block escape valve, got {decision['action']}: {decision.get('reason')}"
        assert decision["reason"] == "sleeping_guard", \
            f"expected reason=sleeping_guard, got {decision.get('reason')}"
        # sleeping_guard 不在 ("no_trigger", "user_busy") → 不累积 held_count/longing
        assert st.cooldown.held_count == 7, \
            f"sleeping_guard must not accumulate held_count, got {st.cooldown.held_count}"
    print("  OK test_sleeping_guard_blocks_escape_valve_at_high_confidence")


def test_escape_valve_sends_when_sleeping_confidence_below_block():
    """v7 对照: 逃生阀激活 + sleeping 置信度 0.85 < 0.9 → 不被 sleeping_guard 拦截，仍 send"""
    from chiguo_daemon import DecisionEngine
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        cfg_path = td_path / "chiguo_proactive.toml"
        cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
        _isolated_toml(cfg_path, td_path)
        engine = DecisionEngine(str(cfg_path), str(td_path / "decisions.jsonl"))
        engine.config["schedule"]["quiet_start"] = 0
        engine.config["schedule"]["quiet_end"] = 0
        engine.state._sync_quiet_window()
        st = engine.state
        st.emotion.anxiety = 100.0
        st.cooldown.last_user_message_at = (datetime.now(CST) - timedelta(days=4)).isoformat()
        st.cooldown.messages_today = 5
        st.cooldown.last_message_at = (datetime.now(CST) - timedelta(hours=2)).isoformat()
        st.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        st.cooldown.event_timestamps = []
        # 0.85 < 0.9（escape_valve_sleep_block 默认）→ 逃生阀豁免成立，不被降级
        st.infer_user_state = lambda now=None, msg_length=None: {
            "posterior": {"sleeping": 0.85, "browsing": 0.1, "busy": 0.05},
            "most_likely": "sleeping", "confidence": 0.85, "utility": 0.1,
            "should_send_bayesian": False, "state_description": "sleeping",
        }
        decision = engine.evaluate()
        assert decision["action"] == "send", \
            f"escape valve should send below sleep block, got {decision['action']}: {decision.get('reason')}"
        assert decision["trigger"] == "longing", decision.get("trigger")
        assert decision["intensity"] == "high"
    print("  OK test_escape_valve_sends_when_sleeping_confidence_below_block")


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════


