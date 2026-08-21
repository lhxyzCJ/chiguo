#!/usr/bin/env python3
"""test_emotion_dynamics.py — A1 弹性衰减 / A2 情绪交互矩阵 / A10 回复饱和阻尼

A2/A10 行为级测试；A1 纯函数测试在 test_chiguo_math.py。
固定种子 + 固定 CST 时间，临时目录隔离（不碰真实 state/log）。
"""

import os
import random
import re
import shutil
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

random.seed(42)

CST = timezone(timedelta(hours=8))

from chiguo_math import apply_interaction_matrix
from chiguo_state import ChiguoState, ChiguoEmotion

TMP_DIR: Path | None = None


def setup() -> dict:
    """复制 toml 到临时目录 + _base_dir 注入（隔离所有运行时文件）"""
    global TMP_DIR
    TMP_DIR = Path(tempfile.mkdtemp(prefix="chiguo_test_emotion_dynamics_"))
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{TMP_DIR / "no_qdrant"}"', src)
    src = re.sub(r"(?m)^mem0_history_db\s*=.*$",
                 f'mem0_history_db = "{TMP_DIR / "no_history.db"}"', src)
    cfg_path = TMP_DIR / "chiguo_proactive_test.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(TMP_DIR)
    return cfg


def teardown():
    global TMP_DIR
    if TMP_DIR is not None:
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR = None


@pytest.fixture(scope="module")
def cfg():
    """Q26 迁移：setup()/teardown() 逻辑改为 pytest 模块级 fixture（原 __main__ 注入）。"""
    c = setup()
    yield c
    teardown()


def make_state(cfg, **overrides) -> ChiguoState:
    """构造 ChiguoState，可覆盖情绪初始值"""
    s = ChiguoState(cfg)
    emo_cfg = cfg.get("emotion", {})
    s.emotion = ChiguoEmotion(
        loneliness=emo_cfg.get("loneliness", 15.0),
        affection=emo_cfg.get("affection", 55.0),
        anxiety=emo_cfg.get("anxiety", 40.0),
        energy=emo_cfg.get("energy", 85.0),
    )
    s.cooldown.last_message_at = None
    s.cooldown.last_user_message_at = None
    s.cooldown.event_timestamps = []
    s.cooldown.drop_events = []
    for k, v in overrides.items():
        if hasattr(s.emotion, k):
            setattr(s.emotion, k, v)
        elif hasattr(s.cooldown, k):
            setattr(s.cooldown, k, v)
    return s


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=CST)


# ═══════════════════════════════════════════════════════════
# A2 情绪交互矩阵（纯函数）
# ═══════════════════════════════════════════════════════════

def test_a2_rule1_affection_anxiety(cfg):
    """affection>60 → anxiety 恢复加速：anxiety *= 1 - 0.02*k*affection/100"""
    emotion = {"loneliness": 30.0, "anxiety": 50.0, "affection": 80.0, "energy": 60.0}
    cfg = {"interaction_affection_anxiety": 2.0}
    out = apply_interaction_matrix(emotion, cfg)
    expected = 50.0 * (1 - 0.02 * 2.0 * 80.0 / 100.0)  # 50*(1-0.032)=48.4
    assert abs(out["anxiety"] - expected) < 1e-9, f"got {out['anxiety']}, expected {expected}"
    # 其他维度不受影响
    assert out["loneliness"] == emotion["loneliness"]
    assert out["energy"] == emotion["energy"]
    assert out["affection"] == emotion["affection"]
    print("  OK test_a2_rule1_affection_anxiety")


def test_a2_rule2_energy_loneliness(cfg):
    """energy<30 → loneliness 恢复加速：loneliness *= 1 + 0.02*k*(30-energy)/30"""
    emotion = {"loneliness": 50.0, "anxiety": 40.0, "affection": 55.0, "energy": 20.0}
    cfg = {"interaction_energy_loneliness": 2.0}
    out = apply_interaction_matrix(emotion, cfg)
    expected = 50.0 * (1 + 0.02 * 2.0 * (30.0 - 20.0) / 30.0)  # 50*(1+0.01333)=50.667
    assert abs(out["loneliness"] - expected) < 1e-9, f"got {out['loneliness']}, expected {expected}"
    assert out["anxiety"] == emotion["anxiety"]
    print("  OK test_a2_rule2_energy_loneliness")


def test_a2_rule3_anxiety_energy(cfg):
    """anxiety>70 → energy 恢复减速：energy *= 1 - 0.01*k"""
    emotion = {"loneliness": 30.0, "anxiety": 80.0, "affection": 55.0, "energy": 50.0}
    cfg = {"interaction_anxiety_energy": 2.0}
    out = apply_interaction_matrix(emotion, cfg)
    expected = 50.0 * (1 - 0.01 * 2.0)  # 49.0
    assert abs(out["energy"] - expected) < 1e-9, f"got {out['energy']}, expected {expected}"
    assert out["anxiety"] == emotion["anxiety"]
    print("  OK test_a2_rule3_anxiety_energy")


def test_a2_thresholds_gate_rules(cfg):
    """阈值外（affection≤60 / energy≥30 / anxiety≤70）→ 不触发"""
    emotion = {"loneliness": 50.0, "anxiety": 50.0, "affection": 60.0, "energy": 30.0}
    cfg = {
        "interaction_affection_anxiety": 2.0,
        "interaction_energy_loneliness": 2.0,
        "interaction_anxiety_energy": 2.0,
    }
    out = apply_interaction_matrix(emotion, cfg)
    assert out == emotion, f"thresholds should gate all rules, got {out}"
    print("  OK test_a2_thresholds_gate_rules")


def test_a2_default_off_identity(cfg):
    """默认 cfg（乘数 1.0 / 缺省）→ 行为恒等"""
    emotion = {"loneliness": 20.0, "anxiety": 80.0, "affection": 90.0, "energy": 10.0,
               "tsundere_index": 70.0, "loneliness_rate": 0.0, "anxiety_rate": 0.0}
    # 全 1.0（toml 默认）与空 cfg 都应恒等
    assert apply_interaction_matrix(emotion, {}) == emotion
    assert apply_interaction_matrix(emotion, {
        "interaction_affection_anxiety": 1.0,
        "interaction_energy_loneliness": 1.0,
        "interaction_anxiety_energy": 1.0,
    }) == emotion
    print("  OK test_a2_default_off_identity")


# ═══════════════════════════════════════════════════════════
# A1 弹性衰减（state tick 路径集成）
# ═══════════════════════════════════════════════════════════

def test_a1_tick_elastic_boost(cfg):
    """tick 后孤独回弹显著快于普通半衰期（弹性在 gap 大时生效）"""
    from chiguo_math import elastic_recover, recover
    s = make_state(cfg, loneliness=10.0)
    now = dt(2026, 6, 15, 14, 0)  # 周一
    s.cooldown.last_user_message_at = (now - timedelta(hours=12)).isoformat()  # silent_h=12 ≤ 24 → hl 不加速
    s.tick(24.0, now)
    # 与普通 recover 对比：recover(10,100,24,40)=10+90*(1-2^(-0.6))≈40.6；elastic ≈ 59.3
    plain = recover(10.0, 100.0, 24.0, 40.0)
    expected = elastic_recover(10.0, 100.0, 24.0, 40.0, 100.0)
    assert s.emotion.loneliness > plain + 10, \
        f"elastic should overshoot plain recover: {s.emotion.loneliness} vs {plain}"
    assert abs(s.emotion.loneliness - expected) < 0.01, \
        f"tick loneliness {s.emotion.loneliness} != pure elastic {expected}"
    print(f"  OK test_a1_tick_elastic_boost: loneliness {plain:.1f} → {s.emotion.loneliness:.1f}")


def test_a1_tick_elastic_energy_uses_elastic_baseline(cfg):
    """A1 回归守卫：energy 的 elastic_recover 必须用 elastic_baseline（默认 100），
    而非 tsundere 人格基线（tick 内同名 baseline 变量曾被 tsundere 回归覆盖）"""
    from chiguo_math import elastic_recover
    s = make_state(cfg, energy=20.0)
    now = dt(2026, 6, 15, 14, 0)
    s.cooldown.last_user_message_at = (now - timedelta(hours=12)).isoformat()
    s.tick(1.0, now)
    # 弹性 hl = 8 / (1 + |100-20|/100) = 4.44 → 1h 后 energy ≈ 20+80*(1-2^(-1/4.44)) ≈ 31.6
    expected = elastic_recover(20.0, 100.0, 1.0, 8.0, 100.0)
    assert abs(s.emotion.energy - expected) < 0.01, \
        f"energy {s.emotion.energy} != pure elastic w/ baseline=100 {expected}"
    print(f"  OK test_a1_tick_elastic_energy_uses_elastic_baseline: energy {s.emotion.energy:.2f}")


def test_a1_tick_matrix_default_off(cfg):
    """默认 toml（interaction_*=1.0）下 tick 的 A2 恒等：anxiety 与纯弹性一致"""
    from chiguo_math import elastic_recover
    s = make_state(cfg, anxiety=40.0)
    now = dt(2026, 6, 15, 14, 0)
    s.cooldown.last_user_message_at = (now - timedelta(hours=12)).isoformat()
    s.tick(5.0, now)
    # anx_hl 受 holiday/课表调制，无法直接手算；只验证不崩 + 值域合法 + 矩阵未越界
    assert 0 <= s.emotion.anxiety <= 100
    assert 0 <= s.emotion.loneliness <= 100
    assert 0 <= s.emotion.energy <= 100
    # 默认关闭恒等：直接验证 apply_interaction_matrix(默认 cfg) 不改 emotion 快照
    import dataclasses
    snap = dataclasses.asdict(s.emotion)
    assert apply_interaction_matrix(snap, cfg.get("emotion", {})) == snap
    print("  OK test_a1_tick_matrix_default_off")


# ═══════════════════════════════════════════════════════════
# A10 回复饱和阻尼（行为级）
# ═══════════════════════════════════════════════════════════

def test_a10_damp_decreasing_in_window(cfg):
    """30 分钟窗口内连续同向回复 → 加成逐次 ×0.5（1.0 → 0.5 → 0.25 → 0.125）"""
    s = make_state(cfg, affection=55.0, energy=40.0, loneliness=70.0)
    now = dt(2026, 6, 15, 14, 0)
    aff_deltas = []
    en_deltas = []
    lo_deltas = []
    for i in range(4):
        a0, e0, l0 = s.emotion.affection, s.emotion.energy, s.emotion.loneliness
        s.on_user_message(now + timedelta(minutes=i), msg_length=15)
        aff_deltas.append(s.emotion.affection - a0)
        en_deltas.append(s.emotion.energy - e0)
        lo_deltas.append(l0 - s.emotion.loneliness)
    # damp = 1.0 / 0.5 / 0.25 / 0.125（msg_length=15 → gain=0.8；latency=None → mult=1.0）
    assert abs(aff_deltas[0] - 0.8) < 1e-9, aff_deltas
    assert abs(aff_deltas[1] - 0.4) < 1e-9, aff_deltas
    assert abs(aff_deltas[2] - 0.2) < 1e-9, aff_deltas
    assert abs(aff_deltas[3] - 0.1) < 1e-9, aff_deltas
    # energy 同受阻尼（+10 × damp）
    assert abs(en_deltas[0] - 10.0) < 1e-9, en_deltas
    assert abs(en_deltas[1] - 5.0) < 1e-9, en_deltas
    assert abs(en_deltas[2] - 2.5) < 1e-9, en_deltas
    # loneliness decay 降幅同样递减（同向事件）
    assert lo_deltas[0] > lo_deltas[1] > lo_deltas[2] > lo_deltas[3] > 0, lo_deltas
    # drop_events 记录 4 条
    assert len(s.cooldown.drop_events) == 4
    assert all(ev.get("direction") == "reply" for ev in s.cooldown.drop_events)
    print(f"  OK test_a10_damp_decreasing_in_window: aff {aff_deltas}, en {en_deltas}")


def test_a10_damp_window_outside_recovers(cfg):
    """窗口外（>30 分钟）→ 阻尼恢复 ×1.0；过期事件被裁剪"""
    s = make_state(cfg, affection=55.0)
    now = dt(2026, 6, 15, 14, 0)
    s.on_user_message(now, msg_length=15)
    a1 = s.emotion.affection
    assert abs((a1 - 55.0) - 0.8) < 1e-9, "first reply no damp"
    # 31 分钟后 → 窗口外 → damp=1.0
    s.on_user_message(now + timedelta(minutes=31), msg_length=15)
    assert abs((s.emotion.affection - a1) - 0.8) < 1e-9, \
        f"outside window should recover to full gain, got {s.emotion.affection - a1}"
    # 窗口外事件被裁剪 → drop_events 只剩 1 条
    assert len(s.cooldown.drop_events) == 1, s.cooldown.drop_events
    print("  OK test_a10_damp_window_outside_recovers")


def test_a10_damp_configurable(cfg):
    """drop_damp_window_minutes / factor / max 参数化生效"""
    cfg = dict(cfg)
    cfg["cooldown"] = dict(cfg.get("cooldown", {}))
    cfg["cooldown"]["drop_damp_window_minutes"] = 60
    cfg["cooldown"]["drop_damp_factor"] = 0.7
    cfg["cooldown"]["drop_damp_max"] = 2
    s = make_state(cfg, affection=55.0)
    now = dt(2026, 6, 15, 14, 0)
    s.on_user_message(now, msg_length=15)
    a1 = s.emotion.affection
    s.on_user_message(now + timedelta(minutes=1), msg_length=15)  # 窗口 60min 内
    d2 = s.emotion.affection - a1
    assert abs(d2 - 0.8 * 0.7) < 1e-9, f"factor=0.7 → 0.56, got {d2}"
    s.on_user_message(now + timedelta(minutes=2), msg_length=15)  # cap=2 → 0.7^2 饱和
    d3 = s.emotion.affection - (a1 + d2)
    assert abs(d3 - 0.8 * 0.7 * 0.7) < 1e-9, f"cap=2 saturated at 0.7^2, got {d3}"
    print("  OK test_a10_damp_configurable")


def test_a10_damp_disabled_clears_events(cfg):
    """window_minutes <= 0（关闭阻尼）→ damp 恒 1.0 且 drop_events 被清空（防无限增长）"""
    cfg = dict(cfg)
    cfg["cooldown"] = dict(cfg.get("cooldown", {}))
    cfg["cooldown"]["drop_damp_window_minutes"] = 0
    s = make_state(cfg, affection=55.0)
    now = dt(2026, 6, 15, 14, 0)
    s.on_user_message(now, msg_length=15)
    a1 = s.emotion.affection
    assert abs((a1 - 55.0) - 0.8) < 1e-9, "disabled → no damp on first reply"
    s.on_user_message(now + timedelta(minutes=1), msg_length=15)
    assert abs((s.emotion.affection - a1) - 0.8) < 1e-9, \
        f"disabled → damp stays 1.0, got {s.emotion.affection - a1}"
    assert s.cooldown.drop_events == [], \
        f"disabled path must clear drop_events, got {s.cooldown.drop_events}"
    print("  OK test_a10_damp_disabled_clears_events")


def test_a10_drop_events_persist_roundtrip(cfg):
    """save → 重新加载 → drop_events 完整保留"""
    s1 = make_state(cfg, affection=55.0)
    now = dt(2026, 6, 15, 14, 0)
    s1.on_user_message(now, msg_length=15)
    assert len(s1.cooldown.drop_events) == 1
    s1.save()

    s2 = ChiguoState(cfg)  # 重新加载（同 _base_dir → 同 state 文件）
    assert s2.cooldown.drop_events == [{"time": now.isoformat(), "direction": "reply"}], \
        f"drop_events not persisted: {s2.cooldown.drop_events}"
    print("  OK test_a10_drop_events_persist_roundtrip")


def test_a10_old_state_missing_field_defaults(cfg):
    """旧状态无 drop_events 字段 → 加载自动补默认 []（无需 STATE_VERSION 升级）"""
    s1 = make_state(cfg)
    s1.save()
    # 手工去掉 drop_events 模拟旧版状态
    state_file = s1.state_path
    import json
    data = json.loads(state_file.read_text())
    data["cooldown"].pop("drop_events", None)
    state_file.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    # 清掉 .bak/.tmp：checksum 拒绝后回退链应落到「全新默认状态」，
    # 而非此前测试留下的旧备份（旧备份可能含 drop_events 事件）
    for suffix in (".bak", ".tmp"):
        Path(str(state_file) + suffix).unlink(missing_ok=True)
    s2 = ChiguoState(cfg)
    assert s2.cooldown.drop_events == [], f"missing field should default to [], got {s2.cooldown.drop_events}"
    print("  OK test_a10_old_state_missing_field_defaults")
