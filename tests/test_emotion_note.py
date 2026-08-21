#!/usr/bin/env python3
"""test_emotion_note.py — 迟菓自身情绪注解表 self_mood_note

参照 test_user_mood.py 结构：纯函数矩阵 + 行为级注入断言。
TDD 全流程：RED（self_mood_note 未实现 → import ImportError）→ GREEN。

契约（与实现对齐，chiguo_math.self_mood_note(emotion: dict) -> str）：
- 开心组合:   energy > 80 且 loneliness < 30 且 affection > 70 → 含「开心」语义
- 委屈难过:   loneliness > 70 且 anxiety > 60                  → 含「委屈/难过」语义
- 高傲娇:     tsundere_index > 80                              → 含「嘴硬/傲娇」语义
- 平淡态:     均不命中 → 空串或最小注解（确定性，不抛异常）
- 优先级:     委屈难过 > 高傲娇 > 开心；多条件同时命中只产出最显者 1-2 条，不冲突堆砌
- 边界:       恰在阈值上（energy=80 / loneliness=30 / affection=70）→ 不命中开心（严格 >/<）
- 防御:       dict 缺键 → 容错不崩溃（仿 user_mood_note 风格）
"""

import os
import re
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_math import self_mood_note
from chiguo_state import ChiguoState, ChiguoEmotion


# ── 纯函数 self_mood_note：组合矩阵 ─────────────────────────────

def test_self_note_happy_combo():
    """开心组合：energy>80 且 loneliness<30 且 affection>70 → 含「开心」语义注解。"""
    note = self_mood_note({"energy": 90.0, "loneliness": 10.0,
                           "affection": 80.0, "anxiety": 30.0,
                           "tsundere_index": 60.0})
    assert note, "开心组合应产出注解"
    assert "开心" in note, note


def test_self_note_sad_combo():
    """委屈难过：loneliness>70 且 anxiety>60 → 含「委屈/难过」语义注解。"""
    note = self_mood_note({"energy": 40.0, "loneliness": 80.0,
                           "affection": 30.0, "anxiety": 70.0,
                           "tsundere_index": 60.0})
    assert note, "委屈难过组合应产出注解"
    assert ("委屈" in note) or ("难过" in note), note


def test_self_note_tsundere():
    """高傲娇：tsundere_index>80 → 含「嘴硬/傲娇」语义注解。"""
    note = self_mood_note({"energy": 75.0, "loneliness": 40.0,
                           "affection": 60.0, "anxiety": 40.0,
                           "tsundere_index": 90.0})
    assert note, "高傲娇应产出注解"
    assert ("嘴硬" in note) or ("傲娇" in note), note


def test_self_note_flat_default():
    """平淡态（默认值附近：energy=85 但 affection=55≤70 等）→ 空串或最小注解，确定性不抛异常。"""
    import random
    random.seed(42)
    flat = {"energy": 85.0, "loneliness": 15.0, "affection": 55.0,
            "anxiety": 40.0, "tsundere_index": 70.0}
    for _ in range(5):
        note = self_mood_note(flat)  # 不抛异常
        assert isinstance(note, str)
    assert self_mood_note(flat) == self_mood_note(flat), "平淡态输出应确定"


def test_self_note_deterministic():
    """同输入两次调用结果一致（确定性，无随机）。"""
    emo = {"energy": 90.0, "loneliness": 10.0, "affection": 80.0,
           "anxiety": 30.0, "tsundere_index": 60.0}
    assert self_mood_note(emo) == self_mood_note(emo)


def test_self_note_missing_keys():
    """输入缺失维度（dict 缺键）→ 防御性处理不崩溃（仿 user_mood_note 容错风格）。"""
    for bad in ({}, {"energy": 90.0}, {"loneliness": 80.0, "anxiety": 70.0},
                {"tsundere_index": 90.0, "energy": 80.0}):
        note = self_mood_note(bad)  # 不得抛异常
        assert isinstance(note, str), f"{bad} → {note!r}"
    # 缺键输出同样确定
    assert self_mood_note({}) == self_mood_note({})


def test_self_note_threshold_boundary():
    """阈值边界锚定：恰在阈值上（energy=80 / loneliness=30 / affection=70）→ 不命中开心（严格 >/<）。"""
    cases = [
        {"energy": 80.0, "loneliness": 10.0, "affection": 80.0},  # energy 恰在阈值
        {"energy": 90.0, "loneliness": 30.0, "affection": 80.0},  # loneliness 恰在阈值
        {"energy": 90.0, "loneliness": 10.0, "affection": 70.0},  # affection 恰在阈值
    ]
    for emo in cases:
        note = self_mood_note(emo)
        assert "开心" not in note, f"{emo} → {note!r}（恰界不应命中开心）"
        assert "委屈" not in note and "难过" not in note, f"{emo} → {note!r}"
        assert "嘴硬" not in note and "傲娇" not in note, f"{emo} → {note!r}"


def test_self_note_priority_no_conflict():
    """多条件同时命中 → 只产出最显者的 1-2 条，不冲突堆砌（无矛盾词并存）。"""
    # 委屈难过 + 高傲娇 同时命中（负面优先）
    emo = {"energy": 50.0, "loneliness": 80.0, "affection": 30.0,
           "anxiety": 70.0, "tsundere_index": 90.0}
    note = self_mood_note(emo)
    assert note, "多条件同时命中应产出注解"
    blocks = [b for b in re.split(r"[；;。\n]", note) if b.strip()]
    assert len(blocks) <= 2, f"最多 1-2 条，实得 {blocks}"
    assert ("委屈" in note) or ("难过" in note), f"负面主导应优先，实得 {note!r}"
    assert not ("开心" in note and ("委屈" in note or "难过" in note)), \
        f"开心与委屈/难过不可冲突堆砌：{note!r}"
    # 开心 + 高傲娇 同时命中
    emo2 = {"energy": 90.0, "loneliness": 10.0, "affection": 80.0,
            "anxiety": 30.0, "tsundere_index": 90.0}
    note2 = self_mood_note(emo2)
    assert note2, "开心+高傲娇应产出注解"
    blocks2 = [b for b in re.split(r"[；;。\n]", note2) if b.strip()]
    assert len(blocks2) <= 2, f"最多 1-2 条，实得 {blocks2}"
    assert ("开心" in note2) or ("嘴硬" in note2) or ("傲娇" in note2), note2
    assert not ("开心" in note2 and ("委屈" in note2 or "难过" in note2)), note2


def test_self_note_mid_lonely():
    """中孤独兜底（⑤）：前四档均不命中但 loneliness>50 → 兜底注解；
    叠加傲娇底色（tsundere_index>80）→ 高傲娇档位优先让位主导，仍共 ≤2 条；
    全不命中 → 空串（确定性，不抛异常）。"""
    # 仅 ⑤ 命中 → 兜底注解
    note = self_mood_note({"energy": 30.0, "loneliness": 60.0, "affection": 20.0,
                           "anxiety": 20.0, "tsundere_index": 30.0})
    assert note, "中孤独兜底应产出注解"
    assert "想哥哥" in note, note
    # ④ 高傲娇与 ⑤ 中孤独同时可命中 → ④ 优先（优先级序），不出现兜底文本
    note2 = self_mood_note({"energy": 30.0, "loneliness": 60.0, "affection": 20.0,
                            "anxiety": 20.0, "tsundere_index": 90.0})
    assert ("嘴硬" in note2) or ("傲娇" in note2), f"高傲娇应优先于中孤独，实得 {note2!r}"
    assert "想哥哥" not in note2, f"高傲娇主导时不应同时出现兜底文本：{note2!r}"
    # 全不命中 → 空串，且确定
    flat = {"energy": 60.0, "loneliness": 40.0, "affection": 50.0,
            "anxiety": 30.0, "tsundere_index": 60.0}
    assert self_mood_note(flat) == "", self_mood_note(flat)
    assert self_mood_note(flat) == self_mood_note(flat), "平淡态输出应确定"


# ── 行为级：注入 _build_context guidance ─────────────────────────

def _make_engine(temp_dir: str):
    """构造临时目录中的 DecisionEngine（隔离配置/日志）。"""
    from chiguo_daemon import DecisionEngine
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return DecisionEngine(str(cfg_path), str(Path(temp_dir) / "chiguo_decisions.jsonl"))


def test_build_context_self_mood_note():
    """_build_context：开关关闭（enabled=0/未配置）→ guidance 与现状恒等（无【自身情绪】标记）；
    开启（enabled=1）且命中开心组合 → guidance 含新注解文本。"""
    from chiguo_trigger import Trigger
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        # 命中开心组合：energy>80 且 loneliness<30 且 affection>70
        st.emotion = ChiguoEmotion(energy=85.0, loneliness=10.0, affection=80.0,
                                   anxiety=30.0, tsundere_index=60.0)
        # 未配置（默认 0）→ 恒等：不注入任何新注解标记
        ctx = engine._build_context(Trigger("lonely_mid"), now, user_state=None)
        assert "【自身情绪】" not in ctx["layer_guidance"], \
            "默认关闭 → guidance 与现状恒等（不含自身情绪注解）"
        # 显式关闭 → 同样恒等
        engine.config["emotion"]["self_mood_note_enabled"] = 0
        ctx = engine._build_context(Trigger("lonely_mid"), now, user_state=None)
        assert "【自身情绪】" not in ctx["layer_guidance"]
        # 开启 → 命中开心组合 → 注入新注解文本
        engine.config["emotion"]["self_mood_note_enabled"] = 1
        ctx = engine._build_context(Trigger("lonely_mid"), now, user_state=None)
        assert "【自身情绪】" in ctx["layer_guidance"], \
            "self_mood_note_enabled=1 且命中开心组合 → guidance 应含自身情绪注解"