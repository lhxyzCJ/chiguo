"""tests/test_v2_domain_relationship.py — v2 domain/relationship 纯函数关系域模型 TDD。

覆盖：中性初值 / 四个事件类型的效应与方向性（initiative_balance 语义）/
recent_warmth 与 recent_tension 半衰回归 / 界内钳制与非法输入容错。
"""
import sys
from dataclasses import FrozenInstanceError, replace
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chiguo_time import CST  # noqa: E402
from domain.relationship import (  # noqa: E402
    RelationshipState,
    apply_event,
    decay,
    initial,
)

NOW = datetime(2026, 6, 16, 14, 0, tzinfo=CST)


# ── initial ─────────────────────────────────────────────────────

def test_initial_neutral_defaults():
    """中性初值：closeness/trust 0.5，其余 0，initiative_balance 中性。"""
    s = initial()
    assert s == RelationshipState()
    assert (s.closeness, s.trust) == (0.5, 0.5)
    assert (s.familiarity, s.recent_warmth, s.recent_tension,
            s.interaction_rhythm, s.initiative_balance,
            s.shared_history_depth) == (0.0,) * 6


def test_state_is_frozen():
    s = initial()
    with pytest.raises(FrozenInstanceError):
        s.closeness = 0.9


# ── message.received ────────────────────────────────────────────

def test_received_warm_message_updates_warmth_closeness_familiarity():
    """用户温暖回复：recent_warmth 上升（按 warmth）、closeness/trust 微升、familiarity 缓增。"""
    s0 = initial()
    s = apply_event(s0, "message.received", now=NOW, payload={"warmth": 1.0})
    assert abs(s.recent_warmth - 0.3) < 1e-12
    assert abs(s.closeness - 0.52) < 1e-12
    assert abs(s.trust - 0.51) < 1e-12
    assert abs(s.familiarity - 0.002) < 1e-12
    assert abs(s.shared_history_depth - 0.001) < 1e-12
    assert s.recent_tension == 0.0
    assert s0 == initial()  # 入参不被修改


def test_received_moves_initiative_toward_user():
    """用户回复 → initiative_balance 向用户侧（-1）移动。"""
    s = apply_event(initial(), "message.received", now=NOW, payload={"warmth": 0.5})
    assert abs(s.initiative_balance - (-0.15)) < 1e-12
    for _ in range(10):
        s = apply_event(s, "message.received", now=NOW, payload={"warmth": 0.5})
    assert s.initiative_balance == -1.0  # 下界钳制


def test_received_cold_message_raises_tension_not_warmth():
    """冷淡回复（warmth<0）：recent_tension 上升、recent_warmth 不变、closeness 微降。"""
    s = initial()
    s = apply_event(s, "message.received", now=NOW, payload={"warmth": -0.5})
    assert abs(s.recent_tension - 0.15) < 1e-12
    assert s.recent_warmth == 0.0
    assert abs(s.closeness - 0.49) < 1e-12
    assert s.trust == 0.5  # 单次冷淡不动长期信任


def test_received_warmth_clamped_to_domain():
    """warmth 超界/非数值 → 钳制与回退，字段不越界。"""
    s = apply_event(initial(), "message.received", now=NOW, payload={"warmth": 99.0})
    assert s.closeness <= 1.0 and s.recent_warmth <= 1.0
    s = apply_event(initial(), "message.received", now=NOW, payload={"warmth": None})
    assert s.recent_warmth == 0.0 and s.recent_tension == 0.0
    s = apply_event(initial(), "message.received", now=NOW, payload=None)
    assert abs(s.familiarity - 0.002) < 1e-12  # 无 payload 也按中性回复记一次


# ── message.sent ────────────────────────────────────────────────

def test_sent_moves_initiative_toward_self():
    """迟菓主动发送 → initiative_balance 向自己侧（+1）移动。"""
    s = apply_event(initial(), "message.sent", now=NOW)
    assert abs(s.initiative_balance - 0.15) < 1e-12
    for _ in range(10):
        s = apply_event(s, "message.sent", now=NOW)
    assert s.initiative_balance == 1.0  # 上界钳制


def test_sent_long_silence_raises_tension():
    """长时间无回复（payload silent_hours ≥ 24）→ recent_tension 缓增；短静默不增。"""
    s = apply_event(initial(), "message.sent", now=NOW,
                    payload={"silent_hours": 48.0})
    assert abs(s.recent_tension - 0.1) < 1e-12
    s = apply_event(initial(), "message.sent", now=NOW,
                    payload={"silent_hours": 3.0})
    assert s.recent_tension == 0.0
    s = apply_event(initial(), "message.sent", now=NOW, payload={})
    assert s.recent_tension == 0.0
    # 持续无回复 → 多次发送累积到上界
    for _ in range(20):
        s = apply_event(s, "message.sent", now=NOW, payload={"silent_hours": 30.0})
    assert s.recent_tension == 1.0


# ── message.delivery_failed ─────────────────────────────────────

def test_delivery_failed_is_identity():
    """未送达不算主动：全字段恒等（不改 warmth、不改 initiative）。"""
    s0 = replace(initial(), recent_warmth=0.4, recent_tension=0.3,
                 initiative_balance=0.5, familiarity=0.2)
    s = apply_event(s0, "message.delivery_failed", now=NOW,
                    payload={"silent_hours": 100.0})
    assert s == s0


def test_unknown_event_is_identity():
    s0 = replace(initial(), recent_warmth=0.4)
    assert apply_event(s0, "weather.changed", now=NOW, payload={"x": 1}) == s0
    assert apply_event(s0, "", now=NOW) == s0


# ── decay ───────────────────────────────────────────────────────

def test_decay_halves_recent_dimensions():
    """半衰回归：recent_warmth 72h 半衰、recent_tension 48h 半衰、rhythm 24h 半衰。"""
    s = replace(initial(), recent_warmth=0.8, recent_tension=0.6,
                interaction_rhythm=0.9, closeness=0.7)
    assert abs(decay(s, 72.0).recent_warmth - 0.4) < 1e-12
    assert abs(decay(s, 48.0).recent_tension - 0.3) < 1e-12
    assert abs(decay(s, 24.0).interaction_rhythm - 0.45) < 1e-12
    d = decay(s, 72.0)
    assert d.closeness == 0.7  # 长期维度不随时衰减
    assert d.initiative_balance == 0.0
    assert decay(s, 0.0) == s
    assert decay(s, -5.0) == s
    assert decay(s, None) == s


def test_decay_tolerates_nan():
    """NaN hours → 视为 0（恒等），不污染状态。"""
    s = replace(initial(), recent_warmth=0.5)
    assert decay(s, float("nan")) == s


# ── 节奏与容错 ───────────────────────────────────────────────────

def test_interaction_rhythm_rises_with_messages():
    """消息事件提升 interaction_rhythm（近期互动密度代理），向 1 收敛。"""
    s = apply_event(initial(), "message.received", now=NOW)
    assert abs(s.interaction_rhythm - 0.05) < 1e-12
    s2 = apply_event(initial(), "message.sent", now=NOW)
    assert abs(s2.interaction_rhythm - 0.05) < 1e-12


def test_invalid_payload_types_tolerated():
    """payload 非 dict / 字段非数值 → 回退默认，不抛异常。"""
    s = apply_event(initial(), "message.received", now=NOW, payload="junk")
    assert abs(s.familiarity - 0.002) < 1e-12
    s = apply_event(initial(), "message.sent", now=NOW,
                    payload={"silent_hours": "long"})
    assert s.recent_tension == 0.0
    s = apply_event(initial(), "message.sent", now=NOW,
                    payload={"silent_hours": float("nan")})
    assert s.recent_tension == 0.0


def test_outputs_stay_in_domain():
    """任意输入下全字段落在文档声明的域内。"""
    s = initial()
    for ev, payload in [
        ("message.received", {"warmth": 5.0}),
        ("message.received", {"warmth": -5.0}),
        ("message.sent", {"silent_hours": 1e9}),
        ("message.delivery_failed", None),
    ]:
        s = apply_event(s, ev, now=NOW, payload=payload)
        assert 0.0 <= s.closeness <= 1.0
        assert 0.0 <= s.trust <= 1.0
        assert 0.0 <= s.familiarity <= 1.0
        assert 0.0 <= s.recent_warmth <= 1.0
        assert 0.0 <= s.recent_tension <= 1.0
        assert 0.0 <= s.interaction_rhythm <= 1.0
        assert 0.0 <= s.shared_history_depth <= 1.0
        assert -1.0 <= s.initiative_balance <= 1.0
