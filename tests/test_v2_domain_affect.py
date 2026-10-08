"""tests/test_v2_domain_affect.py — v2 domain/affect 纯函数情绪域模型 TDD。

对账锚（验收关键）：对同一组输入分别跑旧引擎（ChiguoState 的 EmotionMixin /
InteractionMixin，tmp_path 注入 _base_dir 隔离）与新 domain.affect 纯函数，
断言十个情绪字段在 1e-6 内一致。旧引擎侧无法直接构造的分支（in_class 等）
用显式参数注入对齐。
"""
import math
import random
import re
import sys
import tomllib
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chiguo_state import ChiguoEmotion, ChiguoState  # noqa: E402
from chiguo_time import CST  # noqa: E402
from domain.affect import (  # noqa: E402
    AffectState,
    apply_character_send,
    apply_user_message,
    initial,
    refund_send,
    tick,
)

# 与 ChiguoEmotion 完全一致的十个字段（对账口径）
EMO_FIELDS = (
    "loneliness", "affection", "anxiety", "energy", "tsundere_index",
    "loneliness_rate", "anxiety_rate",
    "baseline_loneliness", "baseline_anxiety", "baseline_affection",
)


def _cfg(tmp_path: Path) -> dict:
    """复制 toml 到 tmp_path 并注入 _base_dir（隔离全部运行时文件）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{tmp_path / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{tmp_path / "no_history.db"}"', src)
    cfg_path = tmp_path / "chiguo_proactive_test.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp_path)
    return cfg


def _dt(*args) -> datetime:
    return datetime(*args, tzinfo=CST)


def _assert_same_emotion(new: AffectState, old: ChiguoState, tol: float = 1e-6):
    for f in EMO_FIELDS:
        a, b = getattr(new, f), getattr(old.emotion, f)
        assert abs(a - b) <= tol, f"{f}: new={a!r} old={b!r}"


def _old_ctx(old: ChiguoState, now: datetime) -> dict:
    """旧引擎情境参数 → 新纯函数显式参数（节假日/课表分支对齐）。"""
    hp = old.holiday_parser
    return {
        "is_holiday": bool(hp.is_holiday(now)) if hp is not None else False,
        "is_school_day": bool(hp.is_school_day(now)) if hp is not None else True,
        "tsundere_baseline": old.personality.tsundere_intensity,
    }


# ── initial ─────────────────────────────────────────────────────

def test_initial_matches_old_constructor(tmp_path):
    """默认初值与旧 ChiguoState 构造结果逐字段一致（含属性派生值）。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    s = initial(cfg)
    _assert_same_emotion(s, old, tol=0.0)
    assert s.noise_loneliness == 0.0 and s.noise_anxiety == 0.0
    assert s.user_mood is None
    assert s.neediness == old.emotion.neediness
    assert s.dominant_layer == old.emotion.dominant_layer


def test_initial_reads_emotion_section(tmp_path):
    """[emotion] 段初值覆盖默认（含 tsundere_index）。"""
    cfg = _cfg(tmp_path)
    cfg["emotion"].update(loneliness=22.0, affection=66.0, anxiety=11.0,
                          energy=77.0, tsundere_index=33.0)
    s = initial(cfg)
    assert (s.loneliness, s.affection, s.anxiety, s.energy, s.tsundere_index) == \
        (22.0, 66.0, 11.0, 77.0, 33.0)
    assert (s.loneliness_rate, s.anxiety_rate) == (0.0, 0.0)
    assert (s.baseline_loneliness, s.baseline_anxiety, s.baseline_affection) == \
        (100.0, 100.0, 0.0)


def test_initial_is_frozen_and_bounds_derived(tmp_path):
    """frozen dataclass：不可赋值；非法初值钳制到合法域。"""
    s = initial({"emotion": {"loneliness": 250.0, "affection": -5.0,
                             "tsundere_index": 999.0}})
    assert s.loneliness == 100.0 and s.affection == 5.0 and s.tsundere_index == 95.0
    with pytest.raises(FrozenInstanceError):
        s.loneliness = 1.0


def test_neediness_and_dominant_layer_layers():
    """dominant_layer 三档阈值与 neediness 公式与旧模型一致。"""
    assert replace(initial({}), anxiety=71.0).dominant_layer == "kernel"
    assert replace(initial({}), loneliness=81.0).dominant_layer == "kernel"
    assert replace(initial({}), loneliness=51.0, anxiety=0.0).dominant_layer == "middle"
    assert replace(initial({}), loneliness=50.0, anxiety=0.0).dominant_layer == "shell"
    s = replace(initial({}), loneliness=80.0, anxiety=50.0, tsundere_index=60.0)
    assert s.neediness == 80.0 * (1 - 60.0 / 200) * 0.5


# ── tick 对账 ────────────────────────────────────────────────────

def test_tick_reconciles_with_old_engine(tmp_path):
    """纯 tick（无噪声）十字段数值对账。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)  # 周二、学期内、非节假日
    old.emotion = ChiguoEmotion(
        loneliness=48.0, affection=52.0, anxiety=61.0, energy=44.0,
        tsundere_index=78.0, baseline_loneliness=90.0,
        baseline_anxiety=95.0, baseline_affection=3.0)
    old.cooldown.last_user_message_at = (now - timedelta(hours=12)).isoformat()
    silent_h = old.cooldown.silent_hours(now)

    new = replace(initial(cfg), loneliness=48.0, affection=52.0, anxiety=61.0,
                  energy=44.0, tsundere_index=78.0, baseline_loneliness=90.0,
                  baseline_anxiety=95.0, baseline_affection=3.0)
    old.tick(6.0, now)
    new = tick(new, 6.0, now, silent_hours=silent_h, config=cfg, **_old_ctx(old, now))
    _assert_same_emotion(new, old)


def test_tick_long_silence_half_life_shortcut(tmp_path):
    """静默 >24h → loneliness 半衰期 ×0.6（长静默分支对账）。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.emotion.loneliness = 30.0
    old.cooldown.last_user_message_at = (now - timedelta(hours=60)).isoformat()
    silent_h = old.cooldown.silent_hours(now)
    assert silent_h > 24  # 前置：确认命中长静默分支
    new = replace(initial(cfg), loneliness=30.0)
    old.tick(5.0, now)
    new = tick(new, 5.0, now, silent_hours=silent_h, config=cfg, **_old_ctx(old, now))
    _assert_same_emotion(new, old)


def test_tick_anxiety_half_life_modulation_reconciles(tmp_path):
    """anxiety 半衰期情境修正：节假日×2.5 / 非上课日×2.0 / 上课×1.8 / 课重×1.4。"""
    cfg = _cfg(tmp_path)
    cases = [
        (_dt(2026, 6, 20, 14, 0), None),                      # 端午（is_holiday）
        (_dt(2026, 6, 27, 14, 0), None),                      # 周六（非上课日）
        (_dt(2026, 6, 16, 14, 0), {"in_class": True, "class_load": "light"}),
        (_dt(2026, 6, 16, 14, 0), {"in_class": False, "class_load": "heavy"}),
    ]
    for now, sch in cases:
        old = ChiguoState(cfg)
        old.emotion.anxiety = 80.0
        old.cooldown.last_user_message_at = (now - timedelta(hours=1)).isoformat()
        if sch is not None:
            old.schedule_status = lambda _now, _sch=sch: _sch
        silent_h = old.cooldown.silent_hours(now)
        new = replace(initial(cfg), anxiety=80.0)
        old.tick(4.0, now)
        new = tick(new, 4.0, now, silent_hours=silent_h, config=cfg,
                   is_holiday=old.holiday_parser.is_holiday(now),
                   is_school_day=old.holiday_parser.is_school_day(now),
                   in_class=bool(sch and sch["in_class"]) if sch else False,
                   class_load=sch.get("class_load") if sch else None,
                   tsundere_baseline=old.personality.tsundere_intensity)
        _assert_same_emotion(new, old)
        assert abs(new.anxiety - 80.0) > 1e-9  # sanity：确实推进了


def test_tick_noise_reconciles_with_old_engine(tmp_path):
    """OU 噪声（noise_state 显式传递 + rng 注入）：多 tick 序列对账。"""
    cfg = _cfg(tmp_path)
    cfg["emotion"]["noise_enabled"] = 1
    cfg["emotion"]["noise_seed"] = 42
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.emotion.loneliness = 60.0
    old.emotion.anxiety = 50.0
    old.cooldown.last_user_message_at = (now - timedelta(hours=3)).isoformat()
    old._noise_x = {"loneliness": 0.0, "anxiety": 0.0}
    old._noise_rng_instance = random.Random(42)

    new = replace(initial(cfg), loneliness=60.0, anxiety=50.0)
    rng = random.Random(42)
    for i in range(3):
        t = now + timedelta(hours=2 * i)
        silent_h = old.cooldown.silent_hours(t)
        old.tick(2.0, t)
        new = tick(new, 2.0, t, silent_hours=silent_h, config=cfg,
                   **_old_ctx(old, t), rng=rng)
        _assert_same_emotion(new, old)
        assert abs(new.noise_loneliness - old._noise_x["loneliness"]) < 1e-12
        assert abs(new.noise_anxiety - old._noise_x["anxiety"]) < 1e-12
    assert new.noise_loneliness != 0.0  # sanity：噪声确实被消费


def test_tick_noise_enabled_without_rng_is_safe(tmp_path):
    """开启噪声但未注入 rng → 自播种（无全局状态污染），值合法。"""
    cfg = _cfg(tmp_path)
    cfg["emotion"]["noise_enabled"] = 1
    before = random.getstate()
    s = tick(replace(initial(cfg), loneliness=60.0), 1.0, _dt(2026, 6, 16, 12, 0),
             silent_hours=999.0, config=cfg)
    assert 0.0 <= s.loneliness <= 100.0
    assert random.getstate() == before  # 不消费全局 random 序列


# ── apply_user_message 对账 ──────────────────────────────────────

def test_apply_user_message_reconciles_with_old_engine(tmp_path):
    """基础回复路径对账：latency 倍率 + A10 damp + analysis 微调 + 个性不安敏感度。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.emotion = ChiguoEmotion(loneliness=70.0, affection=55.0, anxiety=62.0,
                                energy=40.0, tsundere_index=80.0)
    old.cooldown.last_message_at = (now - timedelta(hours=2)).isoformat()
    old.cooldown.drop_events = [{"time": (now - timedelta(minutes=5)).isoformat(),
                                 "direction": "reply"}]
    analysis = {"warmth": -0.4, "effort": 0.6, "attention": 0.2}

    new = replace(initial(cfg), loneliness=70.0, affection=55.0, anxiety=62.0,
                  energy=40.0, tsundere_index=80.0)
    sens = old.personality.anxiety_sensitivity()  # 调用前捕获（调用会演化人格）
    old.on_user_message(now, msg_length=40, analysis=analysis)
    new = apply_user_message(new, now, msg_length=40, latency_hours=2.0,
                             analysis=analysis, config=cfg, damp=0.5,
                             anxiety_sensitivity=sens)
    _assert_same_emotion(new, old)


def test_apply_user_message_fast_latency_and_no_analysis(tmp_path):
    """秒回倍率 + 无 analysis（latency ≤ fast 阈值）。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.cooldown.last_message_at = (now - timedelta(minutes=3)).isoformat()
    new = initial(cfg)
    sens = old.personality.anxiety_sensitivity()
    old.on_user_message(now, msg_length=15)
    new = apply_user_message(new, now, msg_length=15, latency_hours=3.0 / 60.0,
                             config=cfg, damp=1.0, anxiety_sensitivity=sens)
    _assert_same_emotion(new, old)


def test_apply_user_message_very_slow_latency_rebound(tmp_path):
    """很久才回 → affection ×0.4 + anxiety rebound +3（very_slow 分支）。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.cooldown.last_message_at = (now - timedelta(hours=10)).isoformat()
    new = initial(cfg)
    sens = old.personality.anxiety_sensitivity()
    old.on_user_message(now, msg_length=10)
    new = apply_user_message(new, now, msg_length=10, latency_hours=10.0,
                             config=cfg, damp=1.0, anxiety_sensitivity=sens)
    _assert_same_emotion(new, old)
    # sanity：very_slow 的 +3 rebound 生效（同输入、latency=2h 不触发 rebound）
    calm = apply_user_message(initial(cfg), now, msg_length=10, latency_hours=2.0,
                              config=cfg, damp=1.0, anxiety_sensitivity=sens)
    assert new.anxiety > calm.anxiety


def test_apply_user_message_event_delta_and_user_mood_reconciles(tmp_path):
    """EVENT_DELTA + user_mood 影响 + 基线漂移 + 感知 TTL 重放/过期对账（多次调用）。"""
    cfg = _cfg(tmp_path)
    cfg["emotion"].update(event_delta_enabled=True, baseline_drift_rate=1.0,
                          user_mood_low_anxiety_factor=1.0,
                          user_mood_low_affection_factor=1.0)
    old = ChiguoState(cfg)
    old.emotion = ChiguoEmotion(loneliness=60.0, affection=50.0, anxiety=55.0,
                                energy=50.0, tsundere_index=75.0)
    new = replace(initial(cfg), loneliness=60.0, affection=50.0, anxiety=55.0,
                  energy=50.0, tsundere_index=75.0)
    t0 = _dt(2026, 6, 16, 14, 0)

    seq = [
        (t0, {"event_type": "praise", "warmth": 0.5, "attention": 0.5,
              "user_mood": "low", "user_mood_intensity": 0.5}),
        (t0 + timedelta(hours=1), {"warmth": 0.1, "attention": 0.5}),   # 沿用 fresh 感知
        (t0 + timedelta(hours=12), {"warmth": 0.1, "attention": 0.5}),  # 感知过期 → 不重放
    ]
    for now, analysis in seq:
        sens = old.personality.anxiety_sensitivity()  # 调用前捕获（调用会演化人格）
        old.on_user_message(now, msg_length=20, analysis=analysis)
        new = apply_user_message(new, now, msg_length=20, latency_hours=None,
                                 analysis=analysis, config=cfg, damp=1.0,
                                 anxiety_sensitivity=sens)
        _assert_same_emotion(new, old)
        assert new.user_mood == old.cooldown.user_mood


def test_apply_user_message_baseline_drift_only(tmp_path):
    """基线漂移（无 analysis 时 warmth=0）不改变情绪字段，只动 baseline_*。"""
    cfg = _cfg(tmp_path)
    cfg["emotion"]["baseline_drift_rate"] = 1.0
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.cooldown.last_message_at = (now - timedelta(hours=10)).isoformat()
    new = initial(cfg)
    sens = old.personality.anxiety_sensitivity()
    old.on_user_message(now, msg_length=10)
    new = apply_user_message(new, now, msg_length=10, latency_hours=10.0,
                             config=cfg, damp=1.0, anxiety_sensitivity=sens)
    _assert_same_emotion(new, old)
    assert new.baseline_affection < 0.0  # very_slow → affection 基线下降


# ── apply_character_send / refund_send 对账 ──────────────────────

def test_apply_character_send_reconciles_with_old_engine(tmp_path):
    """主动发送记账：energy-20、loneliness 半衰 2h、anxiety+2。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.emotion = ChiguoEmotion(loneliness=64.0, affection=58.0, anxiety=70.0,
                                energy=35.0, tsundere_index=72.0)
    new = replace(initial(cfg), loneliness=64.0, affection=58.0, anxiety=70.0,
                  energy=35.0, tsundere_index=72.0)
    old.on_character_message(now)
    new = apply_character_send(new, config=cfg)
    _assert_same_emotion(new, old)


def test_refund_send_reconciles_with_old_engine(tmp_path):
    """退款数值回滚：energy 回补、anxiety 回减（有在途事件时旧路径执行完整退款）。"""
    cfg = _cfg(tmp_path)
    old = ChiguoState(cfg)
    now = _dt(2026, 6, 16, 14, 0)
    old.emotion = ChiguoEmotion(loneliness=64.0, affection=58.0, anxiety=70.0,
                                energy=35.0, tsundere_index=72.0)
    new = replace(initial(cfg), loneliness=64.0, affection=58.0, anxiety=70.0,
                  energy=35.0, tsundere_index=72.0)
    old.on_character_message(now, trigger_type="lonely_high")
    new = apply_character_send(new, config=cfg)
    old.refund_send(now, None)
    new = refund_send(new, config=cfg)
    _assert_same_emotion(new, old)


def test_refund_send_clamps_energy_cap(tmp_path):
    """退款 energy 封顶 100、anxiety 下限 0。"""
    cfg = _cfg(tmp_path)
    new = replace(initial(cfg), energy=95.0, anxiety=1.0)
    new = refund_send(new, config=cfg)
    assert new.energy == 100.0
    assert new.anxiety == 0.0


def test_apply_character_send_clamps_floor(tmp_path):
    """发送消耗 energy 不低于 0；tsundere/affection 全域合法。"""
    cfg = _cfg(tmp_path)
    new = replace(initial(cfg), energy=5.0, tsundere_index=12.0, affection=6.0)
    new = apply_character_send(new, config=cfg)
    assert new.energy == 0.0
    assert 10.0 <= new.tsundere_index <= 95.0
    assert 5.0 <= new.affection <= 100.0


# ── 容错与边界 ───────────────────────────────────────────────────

def test_tick_tolerates_invalid_inputs(tmp_path):
    """None/NaN 输入回退默认，不抛异常且输出全部落在合法域。"""
    cfg = _cfg(tmp_path)
    s = initial(cfg)
    s = tick(s, None, _dt(2026, 6, 16, 12, 0), silent_hours=None, config=cfg,
             is_holiday=None, is_school_day=None, in_class=None, class_load=123,
             tsundere_baseline=None)
    s = tick(s, float("nan"), _dt(2026, 6, 16, 13, 0),
             silent_hours=float("nan"), config=cfg)
    s = tick(s, -3.0, _dt(2026, 6, 16, 14, 0), silent_hours=-1.0, config=cfg)
    assert 0.0 <= s.loneliness <= 100.0
    assert 5.0 <= s.affection <= 100.0
    assert 0.0 <= s.anxiety <= 100.0
    assert 0.0 <= s.energy <= 100.0
    assert 10.0 <= s.tsundere_index <= 95.0


def test_apply_user_message_tolerates_invalid_inputs(tmp_path):
    """非法 msg_length/damp/latency/analysis 类型 → 回退默认且不抛。"""
    cfg = _cfg(tmp_path)
    s = initial(cfg)
    s = apply_user_message(s, _dt(2026, 6, 16, 14, 0), msg_length=None,
                           latency_hours=float("nan"), analysis="not-a-dict",
                           config=cfg, damp=None)
    assert 0.0 <= s.loneliness <= 100.0
    # analysis 为 dict 但字段非法 → 各字段回退默认
    s = apply_user_message(s, _dt(2026, 6, 16, 15, 0), msg_length=10,
                           analysis={"warmth": None, "effort": "x",
                                     "attention": float("nan"), "event": 3},
                           config=cfg, damp=1.0)
    assert 5.0 <= s.affection <= 100.0
    # config=None 容错
    s = apply_character_send(s, config=None)
    s = refund_send(s, config=None)
    assert 0.0 <= s.energy <= 100.0


def test_tick_invalid_config_values_fall_back(tmp_path):
    """非法 config 值（NaN/字符串/0 半衰期）回退默认，不产生 NaN。"""
    cfg = {}
    cfg["emotion"] = {"loneliness_gain_half_life": "x",
                      "anxiety_gain_half_life": float("nan"),
                      "elastic_baseline": None,
                      "energy_regen_half_life": 0.0,
                      "baseline_forget_half_life": "y"}
    s = replace(initial(cfg), energy=40.0)
    s = tick(s, 3.0, _dt(2026, 6, 16, 12, 0), silent_hours=5.0, config=cfg)
    assert not math.isnan(s.energy) and not math.isnan(s.loneliness)
    assert 0.0 <= s.energy <= 100.0


def test_outputs_are_always_clamped(tmp_path):
    """极端输入下输出仍被钳制到合法域。"""
    cfg = _cfg(tmp_path)
    s = replace(initial(cfg), loneliness=99.9, anxiety=99.9, energy=99.9,
                tsundere_index=94.9, affection=99.9)
    s = tick(s, 100.0, _dt(2026, 6, 16, 12, 0), silent_hours=100.0, config=cfg)
    assert s.loneliness <= 100.0 and s.anxiety <= 100.0
    s = apply_character_send(s, config=cfg)
    assert s.energy <= 100.0
