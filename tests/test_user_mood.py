#!/usr/bin/env python3
"""test_user_mood.py — ① 用户情绪感知接入 needs_care 单元测试（TDD）"""

import os
import re
import sys
import shutil
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

from chiguo_math import user_mood_impact, user_mood_note, mood_fresh
from chiguo_state import ChiguoState, ChiguoEmotion
from chiguo_trigger import evaluate_triggers, Trigger


# ── 纯函数 user_mood_impact ──────────────────────────────────────

def test_impact_calm_or_zero_intensity_empty():
    """calm / intensity<=0 → 零效果（空 dict）。"""
    assert user_mood_impact("calm", 0.8, {}) == {}
    assert user_mood_impact("low", 0.0, {}) == {}
    assert user_mood_impact("low", 0.5, {}) == {}  # 无系数 → 零效果


def test_impact_enabled_coefficients():
    """系数开启 → 精确 delta：low → anxiety +2.0×k×i / affection +0.5×k×i。"""
    cfg = {"user_mood_low_anxiety_factor": 1.0, "user_mood_low_affection_factor": 1.0}
    out = user_mood_impact("low", 0.5, cfg)
    assert abs(out["anxiety"] - 1.0) < 1e-9
    assert abs(out["affection"] - 0.25) < 1e-9


def test_impact_all_moods():
    """五种 mood 的方向表：low/distressed/angry 升 anxiety；happy 升 energy。"""
    cfg = {f"user_mood_{m}_{d}_factor": 1.0
           for m in ("low", "distressed", "happy", "angry")
           for d in ("anxiety", "affection", "energy")}
    assert user_mood_impact("distressed", 1.0, cfg)["anxiety"] == 3.0
    assert user_mood_impact("distressed", 1.0, cfg)["affection"] == 1.0
    assert user_mood_impact("happy", 1.0, cfg)["energy"] == 2.0
    assert user_mood_impact("happy", 1.0, cfg)["affection"] == 1.0
    assert user_mood_impact("angry", 1.0, cfg)["anxiety"] == 2.0
    assert user_mood_impact("angry", 1.0, cfg)["affection"] == -1.0
    # low 的基础值
    assert user_mood_impact("low", 1.0, cfg)["anxiety"] == 2.0
    assert user_mood_impact("low", 1.0, cfg)["affection"] == 0.5


def test_impact_intensity_scales():
    """delta 随 intensity 线性缩放。"""
    cfg = {"user_mood_low_anxiety_factor": 1.0}
    assert user_mood_impact("low", 0.3, cfg)["anxiety"] == 0.6
    assert user_mood_impact("low", 1.0, cfg)["anxiety"] == 2.0


# ── 纯函数 user_mood_note ────────────────────────────────────────

def test_note_calm_empty():
    """calm / 无 mood → 空注解。"""
    assert user_mood_note("calm", 0.8) == ""
    assert user_mood_note("", 0.0) == ""


def test_note_kinds():
    """low/distressed/happy/angry 各有非空注解，且含强度。"""
    for kind in ("low", "distressed", "happy", "angry"):
        note = user_mood_note(kind, 0.6)
        assert note and "0.6" in note, f"{kind}: {note!r}"


# ── 纯函数 mood_fresh（TTL）──────────────────────────────────────

def test_mood_fresh_ttl():
    """TTL 内 fresh（含边界）；过期/None → 不 fresh。"""
    now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
    mood = {"mood": "low", "intensity": 0.7, "at": "2026-08-09T11:00:00+08:00"}
    assert mood_fresh(mood, now, ttl_minutes=120) is True
    assert mood_fresh(mood, now, ttl_minutes=60) is True   # 边界包含
    assert mood_fresh(mood, now, ttl_minutes=59) is False
    assert mood_fresh(None, now, ttl_minutes=120) is False


# ── 行为级：CooldownState.user_mood 消费 ─────────────────────────

def _make_state(temp_dir: str, **emo_overrides) -> ChiguoState:
    """构造临时目录中的 ChiguoState（隔离配置/状态文件）。"""
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(temp_dir)
    s = ChiguoState(cfg)
    if emo_overrides:
        s.emotion = ChiguoEmotion(**emo_overrides)
    return s


def test_consume_mood_tolerance_matrix():
    """容错矩阵：缺键/非法枚举/非数值强度 → 零效果且保留旧感知；显式 calm → 清空。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        # 缺键 → 零效果且不覆盖旧感知
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.5, "at": now.isoformat()}
        st._consume_user_mood({}, now)
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "low", \
            "缺键应保留旧感知"
        # 非法枚举 → 保留旧感知
        st._consume_user_mood({"user_mood": "angsty", "user_mood_intensity": 0.9}, now)
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "low"
        # 强度非数值 → 保留旧感知
        st._consume_user_mood({"user_mood": "low", "user_mood_intensity": "high"}, now)
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "low"
        # 显式 calm → 清空
        st._consume_user_mood({"user_mood": "calm", "user_mood_intensity": 0.5}, now)
        assert st.cooldown.user_mood is None


def test_consume_mood_valid_and_clamp():
    """合法输入写入；强度越界钳制 [0,1]。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st._consume_user_mood({"user_mood": "low", "user_mood_intensity": 0.7}, now)
        m = st.cooldown.user_mood
        assert m and m["mood"] == "low" and abs(m["intensity"] - 0.7) < 1e-9
        assert m["at"] == now.isoformat()
        # 越界钳制
        st._consume_user_mood({"user_mood": "distressed", "user_mood_intensity": 5.0}, now)
        assert st.cooldown.user_mood["intensity"] == 1.0


def test_apply_analysis_impact_applies_mood_delta():
    """_apply_analysis_impact 带 user_mood → 情绪 delta 叠加 + 写入 cooldown。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["user_mood_low_anxiety_factor"] = 1.0
        st.config["emotion"]["user_mood_low_affection_factor"] = 1.0
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        anx0, aff0 = st.emotion.anxiety, st.emotion.affection
        st._apply_analysis_impact(
            {"warmth": 0.0, "effort": 0.0, "attention": 0.5,
             "user_mood": "low", "user_mood_intensity": 0.5}, now)
        assert abs(st.emotion.anxiety - (anx0 + 1.0)) < 1e-9
        assert abs(st.emotion.affection - (aff0 + 0.25)) < 1e-9
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "low"


def test_apply_analysis_impact_ttl_gate_stale_mood():
    """v1.11+R4': 过期 user_mood 不再重放 delta——analysis 无新感知时沿用旧感知,
    若旧感知已过 TTL(默认 6h)→ delta 跳过(修复前无门禁,过期低落 delta 随每条消息无限重放)。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["user_mood_low_anxiety_factor"] = 1.0
        st.config["emotion"]["user_mood_low_affection_factor"] = 1.0
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        # 12h 前(已过期)的低落感知
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.5,
                                 "at": (now - timedelta(hours=12)).isoformat()}
        anx0 = st.emotion.anxiety
        # analysis 无 user_mood 键 → 保留旧感知(过期) → delta 不得重放
        st._apply_analysis_impact({"warmth": 0.0, "effort": 0.0, "attention": 0.5}, now)
        assert abs(st.emotion.anxiety - anx0) < 1e-9, "过期感知不应重放 delta"


def test_apply_analysis_impact_replays_fresh_preserved_mood():
    """v1.11+R4': TTL 内的沿用感知仍重放 delta(有效期内持续影响,门禁只挡过期)。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        st.config["emotion"]["user_mood_low_anxiety_factor"] = 1.0
        st.config["emotion"]["user_mood_low_affection_factor"] = 1.0
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.5,
                                 "at": now.isoformat()}
        anx0 = st.emotion.anxiety
        st._apply_analysis_impact({"warmth": 0.0, "effort": 0.0, "attention": 0.5}, now)
        assert abs(st.emotion.anxiety - (anx0 + 1.0)) < 1e-9, "有效期内沿用感知应重放 delta"


def test_apply_analysis_impact_clamps_boundary():
    """U4 (#232, M-1): recv_dedup 升级路径叠加后越界（anxiety≈99+冷淡→102.3）→
    _apply_analysis_impact 末尾幂等 clamp，save→load 往返后仍 ≤100（修复前存续到
    下次 _finalize 才钳回）。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td, anxiety=99.0, affection=80.0)
        st.config["emotion"]["user_mood_low_anxiety_factor"] = 1.0
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        # 冷淡升级：anxiety≈99 + 低暖度/低努力 → 情绪 delta 会把 anxiety 顶破 100
        st._apply_analysis_impact(
            {"warmth": -0.9, "effort": -0.9, "attention": 0.5,
             "user_mood": "low", "user_mood_intensity": 0.5}, now)
        assert 0 <= st.emotion.anxiety <= 100, f"anxiety 应钳到 [0,100]，实得 {st.emotion.anxiety}"
        assert 0 <= st.emotion.affection <= 100, f"affection 应钳到 [0,100]"
        # sanity：确实在越界钳制生效前会顶破（防线是 clamp 而非不叠加）
        assert st.emotion.anxiety > 99.0, f"应叠加了 delta（才轮到 clamp），实得 {st.emotion.anxiety}"
        # save→load 往返：落盘值在界内（修复前 102.3 落盘存续）
        st.save()
        st2 = ChiguoState(st.config)
        st2._load()
        assert 0 <= st2.emotion.anxiety <= 100, f"落盘后 anxiety 应 ≤100，实得 {st2.emotion.anxiety}"


def test_old_state_missing_user_mood_defaults():
    """旧状态无 user_mood 字段 → 加载补默认 None（drop_events 先例）；save→load 往返保留。"""
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td)
        assert st.cooldown.user_mood is None  # dataclass 默认
        # save→load 往返：感知字段持久化保留
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.5,
                                 "at": "2026-08-09T11:00:00+08:00"}
        st.save()
        st2 = ChiguoState(st.config)
        assert st2.cooldown.user_mood == st.cooldown.user_mood, "user_mood 应持久化保留"


# ── 触发层：comfort 触发 ─────────────────────────────────────────

def test_comfort_trigger_appears_when_enabled():
    """comfort_weight_base>0 + fresh low mood → comfort 被选中；默认 0 → 不出现。

    evaluate_triggers 返回加权随机选中结果（非候选列表）→ 构造"comfort 唯一
    高权重"场景（loneliness/anxiety/energy/affection 均压到不触发），
    random.seed(42) 确定性断言选中类型。
    """
    import random
    random.seed(42)
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td, loneliness=0.0, anxiety=0.0,
                         energy=50.0, affection=50.0)
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.9,
                                 "at": now.isoformat()}
        # 默认关闭 → comfort 不出现在任何选中结果中（跑 20 次采样）
        for _ in range(20):
            r = evaluate_triggers(st, now)
            assert r is None or r.type != "comfort"
        # 开启 → 高权重 comfort 稳定被选中
        st.config["trigger"]["comfort_weight_base"] = 10.0
        st.config["trigger"]["comfort_baseline"] = 0.5
        st.config["trigger"]["comfort_min_weight"] = 0.03
        for _ in range(20):
            r = evaluate_triggers(st, now)
            assert r is not None and r.type == "comfort", \
                f"期望 comfort，实际 {r}"


def test_comfort_weight_monotonic_and_ttl():
    """权重随 intensity 单调（用选中频率对比）；过期 user_mood → 无 comfort。"""
    import random
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td, loneliness=0.0, anxiety=0.0,
                         energy=50.0, affection=50.0)
        st.config["trigger"]["comfort_weight_base"] = 0.3
        st.config["trigger"]["comfort_baseline"] = 0.5
        st.config["trigger"]["comfort_min_weight"] = 0.03
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)

        def hit_rate(intensity):
            random.seed(42)
            st.cooldown.user_mood = {"mood": "low", "intensity": intensity,
                                     "at": now.isoformat()}
            hits = 0
            for _ in range(100):
                r = evaluate_triggers(st, now)
                if r and r.type == "comfort":
                    hits += 1
            return hits

        h_low, h_high = hit_rate(0.3), hit_rate(0.9)
        assert h_high > h_low, f"强度越高选中频率应越高: {h_low} vs {h_high}"
        # 过期（TTL 默认 360min）→ 无 comfort
        random.seed(42)
        st.cooldown.user_mood["at"] = (now - timedelta(hours=12)).isoformat()
        for _ in range(20):
            r = evaluate_triggers(st, now)
            assert r is None or r.type != "comfort"


def test_anxiety_bonus_scales():
    """user_mood_anxiety_bonus>0 + fresh low → anxiety 触发放大（同状态对比）。
    # R7 修正后 A4 为单源 0.75 阈值：free-time 下高焦虑 ≈72+ 才恒必发
    # （实测 anxiety 65/68/70 → 0/60 必发，仅 ≥72 → 100%），bonus 在低焦虑下才有
    # 可观测增益；故用低焦虑 45（w_anx≈0.26 < anxiety_min_weight 0.3 不成候选）
    # 验证开/关效应：base=0，bonus ×1.6 提升 w_anx≈0.36 ≥0.3 成为候选 → boosted>0。"""
    import random
    with tempfile.TemporaryDirectory() as td:
        st = _make_state(td, loneliness=0.0, energy=50.0,
                         affection=50.0, anxiety=45.0)
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        st.cooldown.last_user_message_at = (now - timedelta(hours=1)).isoformat()
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.8,
                                 "at": now.isoformat()}

        def hit_rate():
            random.seed(42)
            hits = 0
            for _ in range(200):
                r = evaluate_triggers(st, now)
                if r and r.type == "anxiety":
                    hits += 1
            return hits

        base = hit_rate()
        assert base == 0, f"无 bonus 时 anxiety=45 应不成候选（<min_weight 0.3）, got {base}"
        st.config["trigger"]["user_mood_anxiety_bonus"] = 2.0
        boosted = hit_rate()
        assert boosted > 0, f"bonus 应提升 anxiety 至候选, got {boosted}"


# ── 集成：mood_note 注入 + recv_dedup 升级路径 ────────────────────

def _make_engine(temp_dir: str):
    """构造临时目录中的 DecisionEngine（隔离配置/日志）。"""
    from chiguo_daemon import DecisionEngine
    src = Path("chiguo_proactive.toml").read_text()
    src = re.sub(r"(?m)^mem0_qdrant_path\s*=.*$",
                 f'mem0_qdrant_path = "{Path(temp_dir) / "no_qdrant"}"', src)
    cfg_path = Path(temp_dir) / "chiguo_proactive.toml"
    cfg_path.write_text(src)
    return DecisionEngine(str(cfg_path), str(Path(temp_dir) / "chiguo_decisions.jsonl"))


def test_build_context_mood_note():
    """_build_context：开关开启 + fresh low mood → guidance 含温柔注解；默认关闭 → 不注入。"""
    from chiguo_trigger import Trigger
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime(2026, 8, 9, 12, 0, tzinfo=CST)
        # 默认关闭（开关=0）→ 即使有 fresh mood 也不注入
        st.cooldown.user_mood = {"mood": "low", "intensity": 0.6,
                                 "at": now.isoformat()}
        ctx = engine._build_context(Trigger("lonely_mid"), now, user_state=None)
        assert "哥哥似乎心情低落" not in ctx["layer_guidance"], \
            "user_mood_note_enabled=0 默认关闭 → 不注入注解"
        # 开启 → 注解注入
        engine.config["trigger"]["user_mood_note_enabled"] = 1
        ctx = engine._build_context(Trigger("lonely_mid"), now, user_state=None)
        assert "哥哥似乎心情低落" in ctx["layer_guidance"], \
            "fresh low mood 应注入温柔语气注解"
        assert "0.6" in ctx["layer_guidance"]
        # needs_care Bayesian 推断 → 追加关心提示
        ctx = engine._build_context(
            Trigger("lonely_mid"), now,
            user_state={"most_likely": "needs_care", "confidence": 0.7})
        assert "哥哥可能需要关心" in ctx["layer_guidance"]
        # 过期 → 注解消失
        st.cooldown.user_mood["at"] = (now - timedelta(hours=12)).isoformat()
        ctx = engine._build_context(Trigger("lonely_mid"), now, user_state=None)
        assert "哥哥似乎心情低落" not in ctx["layer_guidance"]


def test_recv_dedup_upgrade_consumes_user_mood_once():
    """upgrade 路径：bridge 先无分析记录 → 补带 user_mood 的分析 → 只消费一次。

    契约（daemon 注释 v9）：30s 窗口内同文本无分析记录 → 补报升级只叠加分析；
    已升级过的同文本再次上报 → 视为用户真实重发，走完整 on_user_message
    （新分析覆盖旧 user_mood，属既有语义而非重复叠加）。
    """
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        st.config["emotion"]["user_mood_low_anxiety_factor"] = 1.0

        engine.record_user_message("哥哥在吗")
        assert st.cooldown.user_mood is None  # 无分析 → 无感知

        engine.record_user_message(
            "哥哥在吗",
            '{"warmth": 0.0, "effort": 0.0, "attention": 0.5,'
            ' "user_mood": "low", "user_mood_intensity": 0.5}')
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "low"
        assert st.cooldown.recv_dedup.get("analysis") is True

        # 已升级后的同文本再次上报 → 视为真实重发：analysis 重新完整应用（覆盖语义）
        engine.record_user_message(
            "哥哥在吗",
            '{"warmth": 0.0, "effort": 0.0, "attention": 0.5,'
            ' "user_mood": "distressed", "user_mood_intensity": 1.0}')
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "distressed"


def test_recv_dedup_recv_id_precise():
    """U5 (#233, D1): recv_id 精确去重——同 id 无分析→补分析按升级记账；
    不同 id 同文本（或分析已升级过）→ 视为真实重发走完整 on_user_message；无 recv_id 回退窗口逻辑。"""
    with tempfile.TemporaryDirectory() as td:
        engine = _make_engine(td)
        st = engine.state
        now = datetime.now(CST)
        st.cooldown.current_date = now.strftime("%Y-%m-%d")
        st.config["emotion"]["user_mood_low_anxiety_factor"] = 1.0
        rid = "11111111-2222-3333-4444-555555555555"

        # 同 id 两步：先无分析 → 后补分析 → 升级记账（user_mood 落一次，recv_dedup.analysis=True）
        engine.record_user_message("哥哥在吗", recv_id=rid)
        assert st.cooldown.user_mood is None
        assert st.cooldown.recv_dedup.get("recv_id") == rid
        engine.record_user_message(
            "哥哥在吗",
            '{"warmth": 0.0, "effort": 0.0, "attention": 0.5,'
            ' "user_mood": "low", "user_mood_intensity": 0.5}',
            recv_id=rid)
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "low"
        assert st.cooldown.recv_dedup.get("analysis") is True

        # 不同 id 同文本 → 视为真实重发（非升级）：新分析覆盖（distressed 覆盖 low）
        engine.record_user_message(
            "哥哥在吗",
            '{"warmth": 0.0, "effort": 0.0, "attention": 0.5,'
            ' "user_mood": "distressed", "user_mood_intensity": 1.0}',
            recv_id="99999999-0000-0000-0000-555555555555")
        assert st.cooldown.user_mood and st.cooldown.user_mood["mood"] == "distressed",             "不同 id 应走完整 on_user_message（覆盖语义）"

        # 无 recv_id → 回退 text_sha+窗口逻辑（450s 内无分析 → 升级；行为与旧版一致）
        st.cooldown.recv_dedup = {"text_sha": "deadbeef", "at": now.isoformat(),
                                  "analysis": False, "recv_id": None}
        engine.record_user_message(
            "另一次消息",
            '{"warmth": 0.0, "effort": 0.0, "attention": 0.5,'
            ' "user_mood": "low", "user_mood_intensity": 0.5}')
        # text_sha 不匹配 → 非升级，走完整记录
        assert st.cooldown.recv_dedup.get("text_sha") != "deadbeef"
