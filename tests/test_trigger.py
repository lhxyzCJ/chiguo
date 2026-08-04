#!/usr/bin/env python3
"""test_trigger.py — chiguo_trigger 触发器引擎单元测试（v2 sigmoid 加权随机）"""

import os
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_state import ChiguoState
from chiguo_trigger import evaluate_triggers


def _make_state(tmp: str, now: datetime, **overrides) -> ChiguoState:
    """真实 toml 配置 + 临时目录锚定；lancedb 指向不存在路径 → available=False（确定性）"""
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["lancedb_path"] = str(Path(tmp) / "no_lancedb")
    s = ChiguoState(cfg)
    # 默认 10h 前的用户消息 → silent≈6h（睡眠窗口 0-8 抵消 4h），介于 (2,48)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    for k, v in overrides.items():
        if hasattr(s.emotion, k):
            setattr(s.emotion, k, v)
        elif hasattr(s.cooldown, k):
            setattr(s.cooldown, k, v)
    return s


def _run_seeds(s: ChiguoState, now: datetime, n: int = 200, seed0: int = 1000) -> dict:
    """固定种子序列逐一运行 evaluate_triggers → type 计数（确定性，不依赖模块级 RNG）"""
    counts: dict[str, int] = {}
    for i in range(n):
        random.seed(seed0 + i)
        t = evaluate_triggers(s, now)
        key = t.type if t else "None"
        counts[key] = counts.get(key, 0) + 1
    return counts


# ═══════════════════════════════════════════════════════════
# 孤独三级 softmax 归一化竞争
# ═══════════════════════════════════════════════════════════

def test_low_loneliness_never_fires_lonely_or_anxiety():
    """低孤独(15)+默认焦虑(40)：归一化后 lonely/anxiety 均非候选（实测 200 种子 0 次）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        counts = _run_seeds(s, now)
        lonely = sum(v for k, v in counts.items() if k.startswith("lonely_"))
        assert lonely == 0, f"lonely must not fire at loneliness=15, got {counts}"
        assert counts.get("anxiety", 0) == 0, f"anxiety must not fire at anxiety=40, got {counts}"
        # 14:00 无 ritual 候选 + 无记忆 → 唯一候选为 playful（实测 200/200）
        assert counts.get("playful", 0) == 200, f"playful should be sole candidate, got {counts}"
    print("  OK test_low_loneliness_never_fires_lonely_or_anxiety")


def test_no_candidates_returns_none():
    """整体不触发：低孤独 + 低元气（playful 门槛 energy>70 不满足）→ 候选集空 → None"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        counts = _run_seeds(s, now, n=200)
        assert counts.get("None", 0) == 200, f"expected all None, got {counts}"
    print("  OK test_no_candidates_returns_none")


def test_lonely_softmax_competition_at_high_loneliness():
    """高孤独(75)：三级 softmax 归一化互斥竞争（实测 200 种子：low 92 / mid 90 / high 18）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, loneliness=75, energy=40)
        counts = _run_seeds(s, now, n=200)
        lonely = sum(v for k, v in counts.items() if k.startswith("lonely_"))
        assert lonely == 200, f"all triggers should be lonely at loneliness=75, got {counts}"
        # 种子固定为确定性序列 → 三级均非零（实测 200 种子：low 92 / mid 90 / high 18）
        assert counts.get("lonely_low", 0) > 0, f"softmax must spread to low level, got {counts}"
        assert counts.get("lonely_mid", 0) > 0, f"softmax must spread to mid level, got {counts}"
        assert counts.get("lonely_high", 0) > 0, f"softmax must spread to high level, got {counts}"
        assert counts.get("anxiety", 0) == 0, f"anxiety must not fire at anxiety=40, got {counts}"
    print("  OK test_lonely_softmax_competition_at_high_loneliness")


def test_rate_factor_boosts_lonely_at_low_loneliness():
    """暴涨变化率：孤独=15 但 rate=10 → rate_factor=3.55 → lonely_low 进入候选（实测 200/200）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        s.emotion.loneliness_rate = 10.0
        counts = _run_seeds(s, now, n=200)
        assert counts.get("lonely_low", 0) == 200, f"expected all lonely_low, got {counts}"
    print("  OK test_rate_factor_boosts_lonely_at_low_loneliness")


# ═══════════════════════════════════════════════════════════
def test_anxiety_high_fires():
    """anxiety=80：w≈0.65 → 高概率触发（实测 100 种子 88 次 anxiety + 12 次 playful）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, anxiety=80)
        counts = _run_seeds(s, now, n=100)
        assert counts.get("anxiety", 0) >= 50, f"anxiety=80 should fire often, got {counts}"
        assert counts.get("None", 0) == 0, f"anxiety candidate should always exist, got {counts}"
    print("  OK test_anxiety_high_fires")


# ═══════════════════════════════════════════════════════════
# 时间窗口 ritual 触发
# ═══════════════════════════════════════════════════════════

def test_morning_window_probability():
    """早安窗口 09:00（8-10h）：10% 概率触发（实测 200 种子 18 次）；morning_sent 后永不触发"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 9, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        counts = _run_seeds(s, now, n=200)
        morning = counts.get("morning", 0)
        assert 8 <= morning <= 40, f"morning ~20/200 expected, got {counts}"
        assert set(counts) <= {"morning", "None"}, f"only morning/None allowed, got {counts}"
        # morning_sent → 门控关闭
        s.cooldown.morning_sent = True
        counts2 = _run_seeds(s, now, n=50)
        assert counts2.get("morning", 0) == 0, f"morning_sent must suppress, got {counts2}"
    print("  OK test_morning_window_probability")


def test_night_window_probability():
    """晚安窗口 20:00（20-21h）：12% 概率触发（实测 200 种子 24 次）；night_sent 后永不触发"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 20, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        counts = _run_seeds(s, now, n=200)
        night = counts.get("night", 0)
        assert 10 <= night <= 45, f"night ~24/200 expected, got {counts}"
        assert set(counts) <= {"night", "None"}, f"only night/None allowed, got {counts}"
        s.cooldown.night_sent = True
        counts2 = _run_seeds(s, now, n=50)
        assert counts2.get("night", 0) == 0, f"night_sent must suppress, got {counts2}"
    print("  OK test_night_window_probability")


def test_meal_window_probability():
    """饭点 12:00（11/12/17/18/19h）：5% 概率触发（实测 300 种子 12 次）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 12, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        counts = _run_seeds(s, now, n=300)
        meal = counts.get("meal", 0)
        assert 4 <= meal <= 30, f"meal ~15/300 expected, got {counts}"
        assert set(counts) <= {"meal", "None"}, f"only meal/None allowed, got {counts}"
    print("  OK test_meal_window_probability")


# ═══════════════════════════════════════════════════════════
# playful / reflect / longing 候选
# ═══════════════════════════════════════════════════════════

def test_playful_requires_high_energy_and_silence():
    """playful 条件：energy>70 且 2<silent_h<48 且空闲（实测 energy=85/silent≈6h → 200/200）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now)
        counts = _run_seeds(s, now, n=200)
        assert counts.get("playful", 0) == 200, f"playful should be sole candidate, got {counts}"
        # energy=40 → 门槛不满足 → 不触发
        s2 = _make_state(td, now, energy=40)
        counts2 = _run_seeds(s2, now, n=100)
        assert counts2.get("playful", 0) == 0, f"energy=40 must not fire playful, got {counts2}"
        # silent_h>48 → 门槛不满足 → 不触发（无其他候选 → 全 None）
        s3 = _make_state(td, now, energy=85)
        s3.cooldown.last_user_message_at = (now - timedelta(hours=100)).isoformat()
        counts3 = _run_seeds(s3, now, n=100)
        assert counts3.get("playful", 0) == 0, f"silent>48 must not fire playful, got {counts3}"
        assert counts3.get("None", 0) == 100, f"expected all None, got {counts3}"
    print("  OK test_playful_requires_high_energy_and_silence")


def test_reflect_requires_high_affection_low_neuroticism():
    """reflect 条件：affection>70 & silent<2 & energy>60 & neuroticism<70 & 8% 概率门（实测 300 种子 26 次）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, affection=80, energy=85)
        s.cooldown.last_user_message_at = (now - timedelta(minutes=30)).isoformat()
        counts = _run_seeds(s, now, n=300)
        reflect = counts.get("reflect", 0)
        assert 8 <= reflect <= 45, f"reflect ~24/300 expected, got {counts}"
        assert set(counts) <= {"reflect", "None"}, f"only reflect/None allowed, got {counts}"
        # neuroticism=80 → 门槛不满足 → 永不触发
        s2 = _make_state(td, now, affection=80, energy=85)
        s2.cooldown.last_user_message_at = (now - timedelta(minutes=30)).isoformat()
        s2.personality.neuroticism = 80.0
        counts2 = _run_seeds(s2, now, n=100)
        assert counts2.get("reflect", 0) == 0, f"neuroticism=80 must not fire reflect, got {counts2}"
    print("  OK test_reflect_requires_high_affection_low_neuroticism")


def test_longing_overflow_candidate():
    """longing 溢出：held>3 且 λ 累积 ≥ base×1.5 且焦虑不阻塞 → 加权候选（实测 100/100）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        s.cooldown.held_count = 5
        s.cooldown.accumulated_lambda = 0.5
        counts = _run_seeds(s, now, n=100)
        assert counts.get("longing", 0) == 100, f"expected all longing, got {counts}"
        random.seed(1234)
        t = evaluate_triggers(s, now)
        assert t.data.get("held_count") == 5, t.data
        assert t.data.get("accumulated_lambda") == 0.5, t.data
        # 负例：held=2 不溢出 → 无候选 → None
        s2 = _make_state(td, now, energy=40)
        s2.cooldown.held_count = 2
        s2.cooldown.accumulated_lambda = 0.5
        counts2 = _run_seeds(s2, now, n=50)
        assert counts2.get("longing", 0) == 0 and counts2.get("None", 0) == 50, f"got {counts2}"
    print("  OK test_longing_overflow_candidate")


def test_longing_overflow_zero_base_lambda_no_crash():
    """base_lambda=0 时 longing 分支不应 ZeroDivisionError（回归 B4）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        s.cooldown.held_count = 5
        s.cooldown.accumulated_lambda = 0.5
        s.config["poisson"]["base_lambda"] = 0
        t = evaluate_triggers(s, now)
        assert t is None or t.type != "longing", f"base_lambda=0 不应产出 longing, got {t}"
    print("  OK test_longing_overflow_zero_base_lambda_no_crash")


# ═══════════════════════════════════════════════════════════
# 特殊日期 ritual + 记忆触发（tz 防护）
# ═══════════════════════════════════════════════════════════

def test_special_date_ritual():
    """特殊日期(3c 起数据源 = anniversary_mgr 当天匹配,替代 toml special_dates):
    special 高权重候选(实测 100/100);非特殊日不触发"""
    with tempfile.TemporaryDirectory() as td:
        from pathlib import Path as _P
        import json as _json
        _P(td, "anniversaries.json").write_text(_json.dumps({"anniversaries": [
            {"id": "a1", "type": "anniversary", "name": "认识纪念日", "date": "11-03",
             "note": "", "created_at": "2026-01-01"}]}))
        now = datetime(2026, 11, 3, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        counts = _run_seeds(s, now, n=100)
        assert counts.get("special", 0) == 100, f"expected all special, got {counts}"
        now2 = datetime(2026, 11, 4, 14, 0, tzinfo=CST)
        s2 = _make_state(td, now2, energy=40)
        counts2 = _run_seeds(s2, now2, n=50)
        assert counts2.get("special", 0) == 0, f"non-special day must not fire, got {counts2}"
    print("  OK test_special_date_ritual")


def test_memory_reminder_naive_tz_guard():
    """reminder 记忆：naive trigger_at 按 CST 补齐后 ±10min 窗口内触发（实测 50/50）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        naive = datetime(2026, 6, 15, 13, 55).isoformat()  # 无 tzinfo → 按 CST 解析
        s.memories = [{"type": "reminder", "trigger_at": naive, "content": "喝水"}]
        counts = _run_seeds(s, now, n=50)
        assert counts.get("memory", 0) == 50, f"expected all memory, got {counts}"
    print("  OK test_memory_reminder_naive_tz_guard")


def test_memory_reminder_garbage_at_does_not_crash():
    """垃圾 trigger_at 字符串 → 不崩、不触发（实测 30 种子全 None）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        s.memories = [{"type": "reminder", "trigger_at": "garbage", "content": "x"}]
        counts = _run_seeds(s, now, n=30)
        assert counts.get("None", 0) == 30, f"garbage trigger_at must be skipped, got {counts}"
    print("  OK test_memory_reminder_garbage_at_does_not_crash")


def test_memory_habit_window_probability():
    """habit 记忆：窗口小时命中 → 6% 概率触发（实测 300 种子 15 次）"""
    with tempfile.TemporaryDirectory() as td:
        now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
        s = _make_state(td, now, energy=40)
        s.memories = [{"type": "habit", "trigger_window": [14], "content": "背单词"}]
        counts = _run_seeds(s, now, n=300)
        memory = counts.get("memory", 0)
        assert 2 <= memory <= 30, f"habit ~18/300 expected, got {counts}"
    print("  OK test_memory_habit_window_probability")


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("test_trigger.py\n")
    tests = [
        test_low_loneliness_never_fires_lonely_or_anxiety,
        test_no_candidates_returns_none,
        test_lonely_softmax_competition_at_high_loneliness,
        test_rate_factor_boosts_lonely_at_low_loneliness,
        test_anxiety_high_fires,
        test_morning_window_probability,
        test_night_window_probability,
        test_meal_window_probability,
        test_playful_requires_high_energy_and_silence,
        test_reflect_requires_high_affection_low_neuroticism,
        test_longing_overflow_candidate,
        test_special_date_ritual,
        test_memory_reminder_naive_tz_guard,
        test_memory_reminder_garbage_at_does_not_crash,
        test_memory_habit_window_probability,
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
