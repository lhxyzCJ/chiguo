#!/usr/bin/env python3
"""test_adapt_personality.py — 基线回归 + personality_history 测试（Phase 3 任务 9）

测试意图（brief Step 1，按 brief Step 2 注记：用 300 次热情回复断言 >50）：
- 热情回复不甜妹化：无回归时 300 次 WARM+FAST 后 tsundere = 75-0.13*300 = 36 < 50，FAIL 成立
- 持续沉默不极端化：无回归时 200 次 SENT_NO_REPLY 后 tsundere = 75+20 = 95（clamp 90）> 85，FAIL 成立
- regress_rate=0 关闭回归（等价旧行为）
- baseline 记录构造时实际传入值（非 dataclass 默认值），可 reset（加载持久化基线）
"""

import sys, os, shutil, tempfile, tomllib
from pathlib import Path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from chiguo_personality import default_personality, PersonalityDeltas, PersonalityTraits


def _simulate(n_warm=0, n_silent=0, rate=0.01):
    p = default_personality()
    for _ in range(n_warm):
        p.evolve(PersonalityDeltas.WARM_REPLY)
        p.evolve(PersonalityDeltas.FAST_REPLY)
        p.regress_to_baseline(rate)
    for _ in range(n_silent):
        p.evolve(PersonalityDeltas.SENT_NO_REPLY)
        p.regress_to_baseline(rate)
    return p


def test_warm_replies_no_sweet_ification():
    """300 次热情回复后傲娇不应甜妹化（无回归时 tsundere=36，断言 >50 成立）"""
    p = _simulate(n_warm=300)
    assert p.tsundere_intensity > 50, (
        f"300 次热情回复后傲娇应 >50，实际 {p.tsundere_intensity:.1f}"
    )
    assert p.tsundere_intensity > 45, (
        f"300 次热情回复后仍应保持傲娇分支（>45），实际 {p.tsundere_intensity:.1f}"
    )
    print(f"  OK test_warm_replies_no_sweet_ification: tsundere={p.tsundere_intensity:.1f}")


def test_silence_no_extremism():
    """200 次沉默后不应极端化（无回归时 tsundere=90 clamp 上限，断言 <85 成立）"""
    p = _simulate(n_silent=200)
    assert p.tsundere_intensity < 85, (
        f"200 次沉默后傲娇应 <85，实际 {p.tsundere_intensity:.1f}"
    )
    print(f"  OK test_silence_no_extremism: tsundere={p.tsundere_intensity:.1f}")


def test_regress_rate_zero_disables():
    """regress_rate=0 → 回归关闭（等价旧行为，漂移不回收）"""
    p = _simulate(n_warm=300, rate=0.0)
    assert p.tsundere_intensity < 50, (
        f"rate=0 时不应回归，实际 {p.tsundere_intensity:.1f}"
    )
    print(f"  OK test_regress_rate_zero_disables: tsundere={p.tsundere_intensity:.1f}")


def test_regress_to_baseline_moves_toward_initial():
    """回归方向：向构造时的初始值靠拢（v += (baseline - v) * rate）"""
    p = PersonalityTraits(tsundere_intensity=75.0)
    p.evolve(PersonalityDeltas.WARM_REPLY)
    drifted = p.tsundere_intensity
    p.regress_to_baseline(0.5)
    assert drifted < p.tsundere_intensity < 75.0, (
        f"应在漂移值 {drifted:.3f} 与基线 75.0 之间，实际 {p.tsundere_intensity:.3f}"
    )
    print(f"  OK test_regress_to_baseline_moves_toward_initial: {drifted:.3f} → {p.tsundere_intensity:.3f}")


def test_baseline_recorded_from_constructed_values():
    """基线记录构造时实际传入值（非 dataclass 默认值 75.0）"""
    p = PersonalityTraits(tsundere_intensity=42.0)
    p.evolve(PersonalityDeltas.WARM_REPLY)
    p.regress_to_baseline(1.0)
    assert abs(p.tsundere_intensity - 42.0) < 1e-9, f"满速率应回到构造值 42.0，实际 {p.tsundere_intensity}"
    print("  OK test_baseline_recorded_from_constructed_values")


def test_reset_baseline():
    """reset_baseline：加载状态时用持久化基线覆盖（回归目标不随漂移状态漂移）"""
    p = default_personality()
    p.evolve(PersonalityDeltas.SENT_NO_REPLY)
    p.reset_baseline({"tsundere_intensity": 50.0})
    p.regress_to_baseline(1.0)
    assert abs(p.tsundere_intensity - 50.0) < 1e-9, f"重置基线后满速率应到 50.0，实际 {p.tsundere_intensity}"
    print("  OK test_reset_baseline")


# ── state 层接线（adapt_personality 末尾回归 + personality_history 持久化）──

def _make_state(regress_rate=None):
    """最小 config 构造 ChiguoState（临时目录隔离运行时文件）。"""
    from chiguo_state import ChiguoState
    tmp = tempfile.mkdtemp(prefix="chiguo_test_adapt_")
    cfg = {"_base_dir": tmp, "personality": {}}
    if regress_rate is not None:
        cfg["personality"]["regress_rate"] = regress_rate
    return ChiguoState(cfg), tmp


def test_state_adapt_warm_replies_regresses():
    """adapt_personality 末尾应调用基线回归（rate 从 config 读）：300 次热情后 >50"""
    s, tmp = _make_state()
    try:
        for _ in range(300):
            s.adapt_personality({
                "type": "user_reply", "warmth": 0.8,
                "latency_category": "fast", "msg_length": 20,
            })
        assert s.personality.tsundere_intensity > 50, (
            f"300 次热情回复后傲娇应 >50（回归生效），实际 {s.personality.tsundere_intensity:.1f}"
        )
        print(f"  OK test_state_adapt_warm_replies_regresses: tsundere={s.personality.tsundere_intensity:.1f}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_state_regress_rate_zero_disables():
    """config regress_rate=0 → 回归关闭：300 次热情后 <50"""
    s, tmp = _make_state(regress_rate=0.0)
    try:
        for _ in range(300):
            s.adapt_personality({
                "type": "user_reply", "warmth": 0.8,
                "latency_category": "fast", "msg_length": 20,
            })
        assert s.personality.tsundere_intensity < 50, (
            f"regress_rate=0 时不应回归，实际 {s.personality.tsundere_intensity:.1f}"
        )
        print(f"  OK test_state_regress_rate_zero_disables: tsundere={s.personality.tsundere_intensity:.1f}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_personality_history_rolls_to_200():
    """personality_history 每次 adapt 追加 {ts, dims}，上限 200 条滚动"""
    s, tmp = _make_state()
    try:
        for _ in range(300):
            s.adapt_personality({
                "type": "user_reply", "warmth": 0.8,
                "latency_category": "fast", "msg_length": 20,
            })
        hist = s.personality_history
        assert len(hist) == 200, f"应滚动到 200 条，实际 {len(hist)}"
        entry = hist[-1]
        assert set(entry.keys()) == {"ts", "dims"}, f"条目结构应为 {{ts, dims}}，实际 {entry.keys()}"
        assert set(entry["dims"].keys()) == set(PersonalityTraits.__dataclass_fields__), (
            f"dims 应为 8 维全量，实际 {list(entry['dims'].keys())}"
        )
        print(f"  OK test_personality_history_rolls_to_200: len={len(hist)}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_state_persists_baseline_and_history():
    """save/load 往返：personality_baseline 与 personality_history 持久化"""
    from chiguo_state import ChiguoState
    s, tmp = _make_state()
    try:
        for _ in range(5):
            s.adapt_personality({
                "type": "user_reply", "warmth": 0.8,
                "latency_category": "fast", "msg_length": 20,
            })
        baseline = dict(s.personality._baseline)
        history = list(s.personality_history)
        s.save()

        s2 = ChiguoState(cfg_after_save(tmp))
        assert s2.personality._baseline == baseline, (
            f"基线应持久化，实际 {s2.personality._baseline}"
        )
        assert s2.personality_history == history, "history 应持久化"
        # 加载后的人格 = 持久化的漂移值（≠ 基线），回归目标仍是持久化基线
        assert s2.personality.tsundere_intensity < s2.personality._baseline["tsundere_intensity"], (
            f"加载值应低于基线（漂移未回收前），实际 {s2.personality.tsundere_intensity:.2f}"
        )
        print("  OK test_state_persists_baseline_and_history")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def cfg_after_save(tmp: str) -> dict:
    return {"_base_dir": tmp, "personality": {}}


def test_toml_has_regress_rate():
    """chiguo_proactive.toml [personality] 应配置 regress_rate ∈ [0,1]（0=关闭）"""
    with open(Path("chiguo_proactive.toml"), "rb") as f:
        cfg = tomllib.load(f)
    assert "regress_rate" in cfg.get("personality", {}), "toml [personality] 缺 regress_rate"
    rate = cfg["personality"]["regress_rate"]
    assert isinstance(rate, float) and 0.0 <= rate <= 1.0, f"regress_rate 非法: {rate!r}"
    print(f"  OK test_toml_has_regress_rate: regress_rate={rate}")



