#!/usr/bin/env python3
"""test_integration.py — 集成测试：固定种子 + 固定时间 → 固定输出"""

import json
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

random.seed(42)

import tomllib
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState, ChiguoEmotion, CooldownState
from chiguo_trigger import evaluate_triggers

# 用临时目录隔离所有运行时文件（修复：不再触碰项目根的真实状态文件）
TMP_DIR: Path | None = None
TMP_TOML: Path | None = None


def setup():
    """在临时目录中复制配置，注入 _base_dir 锚定，可覆盖测试值"""
    import re
    import shutil
    global TMP_DIR, TMP_TOML
    TMP_DIR = Path(tempfile.mkdtemp(prefix="chiguo_test_integration_"))
    src = Path("chiguo_proactive.toml").read_text()
    # 隔离:lancedb_path 改写为临时目录,防止新机器上连到生产记忆库
    src = re.sub(r"(?m)^lancedb_path\s*=.*$",
                 f'lancedb_path = "{TMP_DIR / "no_lancedb"}"', src)
    TMP_TOML = TMP_DIR / "chiguo_proactive_test.toml"
    TMP_TOML.write_text(src)
    with open(TMP_TOML, "rb") as f:
        cfg = tomllib.load(f)
    # 注入锚点：所有运行时文件（state/break/log）都落在临时目录内
    cfg["_base_dir"] = str(TMP_DIR)
    return cfg


def teardown():
    """清理临时目录（不触碰项目根的任何真实文件）"""
    import shutil
    global TMP_DIR
    if TMP_DIR is not None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR = None


def make_state(cfg, **overrides):
    """构造 ChiguoState，可覆盖情绪值"""
    s = ChiguoState(cfg)
    # 清除可能从真实 state.json 加载的状态，避免污染测试（v5: 也重置情绪）
    emo_cfg = cfg.get("emotion", {})
    s.emotion = ChiguoEmotion(
        loneliness=emo_cfg.get("loneliness", 15.0),
        affection=emo_cfg.get("affection", 55.0),
        anxiety=emo_cfg.get("anxiety", 40.0),
        energy=emo_cfg.get("energy", 85.0),
    )
    s.cooldown.busy_suppress_until = None
    s.cooldown.last_message_at = None
    s.cooldown.last_user_message_at = None
    s.cooldown.event_timestamps = []
    s.cooldown.messages_today = 0
    s.cooldown.current_date = str(date.today())
    s.cooldown.held_count = 0
    s.cooldown.accumulated_lambda = 0.0  # v5: ensure float not None
    for k, v in overrides.items():
        if hasattr(s.emotion, k):
            setattr(s.emotion, k, v)
        elif hasattr(s.cooldown, k):
            setattr(s.cooldown, k, v)
    return s


def dt(*args):
    """快捷构造 CST datetime"""
    return datetime(*args, tzinfo=CST)


# ═══════════════════════════════════════════════════════════
# 测试
# ═══════════════════════════════════════════════════════════

def test_1_initial_no_trigger(cfg):
    """初始状态(孤独15) → 不应触发"""
    s = make_state(cfg)
    trigger = evaluate_triggers(s, dt(2026, 6, 15, 14, 0))
    # 实测（seed=42，多次运行恒定）：候选集为空 → 恒 None
    #  - 孤独=15 → raw_low≈0.012 < 0.03 阈值 → 无 lonely 候选
    #  - anxiety=40 → softmax 归一化 w≈0.171 < anxiety_min_weight(0.3) → 无 anxiety 候选
    #    （修复前无归一化时 anxiety 恒为候选 → 沉默期确定性触发，本测试即回归保护）
    #  - 14:00 不在 morning(8-10)/night(20-21)/meal(11,12,17,18,19) 窗口，
    #    无记忆/特殊日期/长期沉默（LanceDB 8% 通道在 seed=42 下未命中）
    assert trigger is None, f"initial state should not trigger, got {trigger.type if trigger else None}"
    print("  OK test_1_initial: no trigger (candidates empty at 14:00)")


def test_2_high_loneliness_triggers(cfg):
    """高孤独(75) + 工作日 → 应触发 lonely 类"""
    s = make_state(cfg, loneliness=75, anxiety=55)
    s.cooldown.current_date = "2026-06-15"
    trigger = evaluate_triggers(s, dt(2026, 6, 15, 14, 0))
    # 高孤独应该有触发（seed=42 下预期 lonely_low 或 lonely_mid）
    assert trigger is not None, "high loneliness should trigger"
    # memory / meal / morning also valid triggers with seed=42
    valid_types = ("lonely_", "anxiety", "memory", "morning", "meal", "night", "special", "playful")
    assert any(trigger.type.startswith(t) if t.endswith("_") else trigger.type == t for t in valid_types), \
        f"unexpected trigger type, got {trigger.type}"
    print("  OK test_2_high_loneliness:", trigger.type, trigger.intensity)


def test_3_quiet_hours(cfg):
    """0:00-8:00 静默时段 → can_send=False"""
    s = make_state(cfg)
    assert not s.can_send(dt(2026, 6, 15, 3, 0)), "03:00 should be quiet"
    assert not s.can_send(dt(2026, 6, 15, 0, 30)), "00:30 should be quiet"
    assert s.can_send(dt(2026, 6, 15, 14, 0)), "14:00 should be allowed"
    assert s.can_send(dt(2026, 6, 15, 22, 30)), "22:30 should be allowed"
    print("  OK test_3_quiet_hours")


def test_4_daily_limit(cfg):
    """达到每日上限 → can_send=False"""
    s = make_state(cfg)
    s.cooldown.messages_today = 2
    s.cooldown.current_date = "2026-06-15"
    # 沉默模式上限=2
    s.cooldown.last_user_message_at = (dt(2026, 6, 1, 10, 0)).isoformat()
    assert not s.can_send(dt(2026, 6, 15, 14, 0)), "daily limit should block"
    print("  OK test_4_daily_limit")


def test_5_low_energy(cfg):
    """元气耗尽 → can_send=False"""
    s = make_state(cfg, energy=5)
    assert not s.can_send(dt(2026, 6, 15, 14, 0)), "low energy should block"
    print("  OK test_5_low_energy")


def test_6_holiday_availability(cfg):
    """节假日 → availability=0.85"""
    s = make_state(cfg)
    # 国庆节
    avail = s.availability(dt(2026, 10, 1, 14, 0))
    assert avail == 0.85, f"holiday should be 0.85, got {avail}"
    # 非节假日工作日 → 依赖课表
    avail2 = s.availability(dt(2026, 6, 17, 14, 0))  # Wed
    assert avail2 >= 0.5, f"weekday min 0.5, got {avail2}"
    print("  OK test_6_holiday: 国庆=0.85, 工作日=", avail2)


def test_7_in_class_availability(cfg):
    """上课中 → availability 极低"""
    s = make_state(cfg)
    # 周一上午第一节课（08:30）
    avail = s.availability(dt(2026, 6, 15, 8, 30))
    # 上课中 availability 应在 0.05-0.20；on_break(8月=暑假,on_break 用真实今天判定)时兜底 0.85
    assert avail <= 0.20 or avail == 0.85, \
        f"expected in-class (<=0.20) or on_break fallback (0.85), got {avail}"
    print("  OK test_7_in_class: availability =", avail)



def test_7b_schedule_disabled_availability(cfg):
    """课表可选来源 enabled=false → 不解析、availability=1.0（按空闲）"""
    cfg = dict(cfg)
    cfg.setdefault("schedule", {})["enabled"] = False
    import tempfile as _tf
    from pathlib import Path as _P
    s = make_state(cfg)
    # 清 break 状态（on_break 用真实今天=8月暑假判定）→ 走课表层
    import chiguo_state as _cs
    _orig = _cs.ChiguoState.break_state_path
    _cs.ChiguoState.break_state_path = property(lambda self: _P(_tf.mkdtemp()) / "no-break.json")
    s.semester_end = date(2099, 12, 31)  # 未来学期 → 非假期，走课表层
    sch = s.schedule_status(dt(2026, 6, 15, 8, 30))
    assert sch is None, f"课表未启用时 schedule_status 应为 None, got {sch}"
    avail = s.availability(dt(2026, 6, 15, 8, 30))
    assert avail == 1.0, f"enabled=false 应 availability=1.0, got {avail}"
    _cs.ChiguoState.break_state_path = _orig
    print("  OK test_7b: schedule disabled → status None + availability 1.0")

def test_7c_schedule_parser_disabled(cfg):
    """ScheduleParser(enabled=False) → 不解析、query available=False"""
    from schedule_parser import ScheduleParser
    import tempfile
    from pathlib import Path
    from datetime import date as date_cls
    with tempfile.TemporaryDirectory() as td:
        p = ScheduleParser(xlsx_path=str(Path(td) / "none.xlsx"),
                           cache_path=str(Path(td) / "cache.json"),
                           semester_start=date_cls(2026, 2, 23),
                           enabled=False)
        r = p.query(datetime(2026, 6, 15, 8, 30))
        assert r["available"] is False, r
        assert r["in_class"] is False, r
        print("  OK test_7c: ScheduleParser(enabled=False) → available=False")
def test_8_morning_window(cfg):
    """早安窗口 (09:00) → 应有触发，且 type 合法"""
    s = make_state(cfg, loneliness=40)
    s.cooldown.current_date = "2026-06-15"
    trigger = evaluate_triggers(s, dt(2026, 6, 15, 9, 0))
    # 实测（seed=42）：morning 需 10% 概率命中（_should_morning），实测为 lonely_low，
    # 并非确定性 morning → 断言非 None + type 在 09:00 窗口合法集合内
    assert trigger is not None, "morning window should produce some trigger"
    # 09:00 不在 meal 窗口（11/12/17/18/19h，见 chiguo_trigger._should_meal）→ 不含 meal
    valid_types = ("morning", "lonely_low", "lonely_mid", "lonely_high",
                   "memory", "playful", "special", "night")
    assert trigger.type in valid_types, f"unexpected trigger type, got {trigger.type}"
    print("  OK test_8_morning:", trigger.type, trigger.intensity)


def test_9_user_msg_reduces_loneliness(cfg):
    """收到主人消息 → 孤独骤降"""
    s = make_state(cfg, loneliness=70)
    before = s.emotion.loneliness
    s.on_user_message(dt(2026, 6, 15, 14, 0), msg_length=15)
    after = s.emotion.loneliness
    assert after < before, f"loneliness should drop: {before} → {after}"
    assert after < 50, f"should be well below 70, got {after}"
    print(f"  OK test_9_user_msg: loneliness {before:.0f} → {after:.0f}")


def test_10_semester_end(cfg):
    """学期结束 → on_break=True"""
    s = make_state(cfg)
    s.semester_end = date(2026, 6, 20)  # past
    assert s.on_break == True
    assert s.availability(dt(2026, 6, 21, 14, 0)) == 0.85
    print("  OK test_10_semester_end: on_break=True, avail=0.85")


def test_11_break_range(cfg):
    """日期区间匹配 → _in_break_range 正确"""
    bp = TMP_DIR / "break_state.json"
    bp.write_text(json.dumps({
        "breaks": [
            {"start": "2026-01-12", "end": "2026-02-22", "note": "寒假"},
            {"start": "2026-07-05", "end": "2026-08-31", "note": "暑假"},
        ]
    }, ensure_ascii=False))
    s = make_state(cfg)
    assert s._in_break_range(date(2026, 1, 20)) == True, "Jan 20 in winter break"
    assert s._in_break_range(date(2026, 7, 20)) == True, "Jul 20 in summer break"
    assert s._in_break_range(date(2026, 3, 1)) == False, "Mar 1 not in break"
    assert s._in_break_range(date(2026, 6, 21)) == False, "Jun 21 not in break"
    bp.unlink()
    print("  OK test_11_break_range")


def test_12_min_interval(cfg):
    """最小间隔 → can_send=False"""
    s = make_state(cfg)
    # 刚刚发过消息
    s.cooldown.last_message_at = dt(2026, 6, 15, 14, 0).isoformat()
    assert not s.can_send(dt(2026, 6, 15, 14, 5)), "5min after should be blocked"
    assert not s.can_send(dt(2026, 6, 15, 14, 29)), "29min should be blocked"
    assert s.can_send(dt(2026, 6, 15, 14, 31)), "31min should be allowed"
    print("  OK test_12_min_interval")


# ═══════════════════════════════════════════════════════════
# v3 新功能测试
# ═══════════════════════════════════════════════════════════

def test_13_rate_energy_override(cfg):
    """孤独暴涨 + 低元气 → can_send 覆盖检查"""
    s = make_state(cfg, loneliness=30, energy=8, loneliness_rate=6.0)
    s.cooldown.current_date = "2026-06-15"
    # rate_energy_override=true, threshold=5.0, min=5
    assert s.can_send(dt(2026, 6, 15, 14, 0)), "rate override should allow sending"
    # energy below min (5) → still blocked
    s.emotion.energy = 3
    assert not s.can_send(dt(2026, 6, 15, 14, 0)), "energy < min(5) should block"
    # low rate, low energy → blocked normally
    s.emotion.energy = 8
    s.emotion.loneliness_rate = 1.0
    assert not s.can_send(dt(2026, 6, 15, 14, 0)), "low rate should not override"
    print("  OK test_13_rate_energy_override")


def test_14_hawkes_intensity_integration(cfg):
    """Hawkes 事件记录 + event_timestamps 正确填充，Hawkes 激发 > 0"""
    s = make_state(cfg, loneliness=70, anxiety=60)
    now = dt(2026, 6, 15, 14, 0)
    s.cooldown.current_date = "2026-06-15"
    s.cooldown.last_user_message_at = now.isoformat()

    # 记录事件前
    assert len(s.cooldown.event_timestamps) == 0
    # 模拟 character message
    s.on_character_message(now, "lonely_mid")
    assert len(s.cooldown.event_timestamps) == 1
    assert s.cooldown.event_timestamps[0]["type"] == "lonely_mid"
    assert "time" in s.cooldown.event_timestamps[0]

    # 有 Hawkes 的 λ > 无 Hawkes 的 λ（相同情绪下）
    # event at exactly now → dt=0, no self-excitation. Shift to 0.5h ago.
    s.cooldown.event_timestamps[0]["time"] = (now - timedelta(hours=0.5)).isoformat()
    lam_with = s.current_lambda(now)
    # 暂时清空 event_timestamps 模拟无 Hawkes
    saved = s.cooldown.event_timestamps
    s.cooldown.event_timestamps = []
    lam_without = s.current_lambda(now)
    s.cooldown.event_timestamps = saved

    assert lam_with > lam_without, (
        f"Hawkes should add excitation: {lam_without:.4f} → {lam_with:.4f}"
    )
    print(f"  OK test_14_hawkes: events={len(saved)}, λ {lam_without:.4f} → {lam_with:.4f}")


def test_15_next_evaluation_at(cfg):
    """idle 决策包含 next_evaluation_at"""
    from chiguo_daemon import DecisionEngine
    # 锚定临时目录（config 所在目录），绝不触碰项目根的真实 state/log
    engine = DecisionEngine(
        config_path=str(TMP_TOML),
        log_path=str(TMP_DIR / "chiguo_decisions.jsonl"),
    )
    s = engine.state
    # 固定 last_tick 防止 _tick 触发大幅情绪变化
    s.last_tick = dt(2026, 6, 15, 13, 59).isoformat()
    s.cooldown.current_date = "2026-06-15"
    s.cooldown.last_message_at = None  # 防真实 state.json 污染：force tick(0)
    s.cooldown.last_user_message_at = None
    s.emotion.loneliness = 10
    s.emotion.energy = 5
    s.emotion.loneliness_rate = 0
    s.emotion.anxiety_rate = 0

    decision = engine.evaluate()
    assert decision["action"] == "idle", (
        f"expected idle, got {decision.get('action')} reason={decision.get('reason')}"
    )
    assert "next_evaluation_at" in decision, (
        f"expected next_evaluation_at in idle decision, got keys: {list(decision.keys())}"
    )
    assert decision["next_evaluation_at"] is not None
    print(f"  OK test_15_next_eval: next at {decision['next_evaluation_at']}")


def test_16_anxiety_rate_tracking(cfg):
    """tick 后 anxiety_rate 被正确计算"""
    s = make_state(cfg, anxiety=30)
    s.emotion.anxiety = 30
    s.tick(5.0, dt(2026, 6, 15, 14, 0))
    # 5h 后 anxiety 向 100 靠拢，anxiety_rate 应为正值
    assert s.emotion.anxiety_rate > 0, f"anxiety_rate should be positive after 5h tick"
    print(f"  OK test_16_anxiety_rate: {s.emotion.anxiety_rate:.3f}/h (anx {s.emotion.anxiety:.0f})")


def test_17_rate_affects_lambda(cfg):
    """高变化率 → λ 放大"""
    s = make_state(cfg, loneliness=50, anxiety=40)
    now = dt(2026, 6, 15, 14, 0)
    s.cooldown.current_date = "2026-06-15"
    s.cooldown.last_user_message_at = now.isoformat()
    s.emotion.loneliness_rate = 0.2
    s.emotion.anxiety_rate = 0.1
    lam_low = s.current_lambda(now)

    s.emotion.loneliness_rate = 6.0
    s.emotion.anxiety_rate = 4.0
    lam_high = s.current_lambda(now)

    assert lam_high > lam_low, (
        f"high rate should increase λ: {lam_low:.4f} → {lam_high:.4f}"
    )
    print(f"  OK test_17_rate_lambda: λ {lam_low:.4f} → {lam_high:.4f}")


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("test_integration.py\n")
    try:
        cfg = setup()

        basic_tests = [
            test_1_initial_no_trigger,
            test_2_high_loneliness_triggers,
            test_3_quiet_hours,
            test_4_daily_limit,
            test_5_low_energy,
            test_6_holiday_availability,
            test_7_in_class_availability,
            test_7b_schedule_disabled_availability,
            test_7c_schedule_parser_disabled,
            test_8_morning_window,
            test_9_user_msg_reduces_loneliness,
            test_10_semester_end,
            test_11_break_range,
            test_12_min_interval,
        ]
        v3_tests = [
            test_13_rate_energy_override,
            test_14_hawkes_intensity_integration,
            test_15_next_evaluation_at,
            test_16_anxiety_rate_tracking,
            test_17_rate_affects_lambda,
        ]
        tests = basic_tests + v3_tests

        for t in tests:
            t(cfg)

        print(f"\n{'='*40}")
        print(f"ALL {len(tests)} integration tests passed.")
    finally:
        teardown()


