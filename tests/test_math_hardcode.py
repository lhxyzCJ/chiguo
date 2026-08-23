#!/usr/bin/env python3
"""test_math_hardcode.py — T08 math 收敛 TDD (RED→GREEN)

验收：chiguo_trigger 中 rate_factor/tsundere/w阈/longing/jitter 硬编码可被 CONFIG 覆盖；
chiguo_math.dynamic_lambda loneliness_k 默认 0.08 (与 toml 一致)；
jitter 用隔离 Random 实例，不污染全局 random；
cfg_float 单源 + _clamp01 钳制。
"""
import math
import os
import random
import sys
import tempfile
import tomllib
from datetime import datetime, timezone, timedelta
from pathlib import Path
import inspect

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CST = timezone(timedelta(hours=8))

import chiguo_math
import chiguo_trigger


# ── helpers ──────────────────────────────────────────────────
def _make_state(tmp: str, now: datetime, **overrides):
    cfg_path = Path(tmp) / "chiguo_proactive.toml"
    cfg_path.write_text(Path("chiguo_proactive.toml").read_text())
    with open(cfg_path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_base_dir"] = str(tmp)
    cfg["memory"]["mem0_qdrant_path"] = str(Path(tmp) / "no_qdrant")
    cfg["memory"]["mem0_history_db"] = str(Path(tmp) / "no_history.db")
    os.environ["CHIGUO_MEM0_DISABLED"] = "1"
    from chiguo_state import ChiguoState
    s = ChiguoState(cfg)
    s.cooldown.last_user_message_at = (now - timedelta(hours=10)).isoformat()
    s.cooldown.current_date = now.strftime("%Y-%m-%d")
    for k, v in overrides.items():
        if hasattr(s.emotion, k):
            setattr(s.emotion, k, v)
    return s


# ── 1. chiguo_math dynamic_lambda 默认值 ─────────────────────
def test_dynamic_lambda_loneliness_k_default_is_008():
    """dynamic_lambda loneliness_k 默认 0.1 与 toml 0.08 不一致 → 必须统一 0.08"""
    sig = inspect.signature(chiguo_math.dynamic_lambda)
    default_k = sig.parameters["loneliness_k"].default
    assert default_k == 0.08, f"loneliness_k 默认应为 0.08，实为 {default_k}"

    # 行为锚定：toml 的 lambda_loneliness_k=0.08，用默认参数计算应一致
    # dynamic_lambda(loneliness=60, anxiety=50) with default k vs explicit 0.08 必须一致
    v_default = chiguo_math.dynamic_lambda(60, 50)
    v_explicit = chiguo_math.dynamic_lambda(60, 50, loneliness_k=0.08)
    assert v_default == v_explicit, f"default 与 0.08 显式不一致: {v_default} vs {v_explicit}"
    # 与 0.1 的旧默认值应不一致（确保已修复）
    v_old = chiguo_math.dynamic_lambda(60, 50, loneliness_k=0.1)
    assert v_default != v_old, "默认仍等于旧值 0.1，说明未修复"


def test_sigmoid_defaults_annotated_as_fallback():
    """sigmoid midpoint=50 steepness=0.1 默认保留，但须标注 fallback 注释"""
    src = Path("chiguo_math.py").read_text(encoding="utf-8")
    # 检查 sigmoid 定义行附近有 fallback / CONFIG 字样注释
    lines = src.splitlines()
    sig_line = next((l for l in lines if "def sigmoid" in l), "")
    assert "50" in sig_line and "0.1" in sig_line, f"sigmoid 定义异常: {sig_line}"
    # 要求文件内在 sigmoid 附近有 fallback 标注
    idx = lines.index(sig_line) if sig_line in lines else -1
    ctx = "\n".join(lines[max(0, idx-2): idx+3])
    assert "fallback" in ctx.lower() or "config" in ctx.lower() or "CONFIG" in ctx, \
        "sigmoid 默认值需标注为 fallback/CONFIG"


# ── 2. rate_factor 可被 CONFIG 覆盖 ──────────────────────────
def test_rate_factor_configurable():
    """rate_factor 1.5*0.3/2.0*0.2 必须走 CONFIG；改大阈值/改小因子应显著抑制 lonely 触发"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        # 对照组：默认配置 + 暴涨 rate → 应触发 lonely
        s_base = _make_state(td, now, energy=40)
        s_base.emotion.loneliness_rate = 10.0
        s_base.emotion.anxiety_rate = 10.0
        # 统计 100 种子下 lonely 触发率
        cnt_base = 0
        for i in range(100):
            random.seed(1000+i)
            t = chiguo_trigger.evaluate_triggers(s_base, now)
            if t and t.type.startswith("lonely_"):
                cnt_base += 1
        assert cnt_base >= 80, f"基线暴涨应高频触发 lonely，实 {cnt_base}/100"

        # 实验组：把阈值抬到 100（远超 rate=10）→ rate_factor 恒 1.0 → lonely 应大量消失或显著下降
        # 通过写 CONFIG 覆盖阈值
        s_cfg = _make_state(td, now, energy=40)
        s_cfg.emotion.loneliness_rate = 10.0
        s_cfg.emotion.anxiety_rate = 10.0
        # 新 CONFIG 键（T08 新增）
        s_cfg.config.setdefault("trigger", {})["lonely_rate_lo_threshold"] = 100.0
        s_cfg.config["trigger"]["lonely_rate_anx_threshold"] = 100.0
        # 因子也置零更保险
        s_cfg.config["trigger"]["lonely_rate_lo_factor"] = 0.0
        s_cfg.config["trigger"]["lonely_rate_anx_factor"] = 0.0
        cnt_cfg = 0
        for i in range(100):
            random.seed(1000+i)
            t = chiguo_trigger.evaluate_triggers(s_cfg, now)
            if t and t.type.startswith("lonely_"):
                cnt_cfg += 1
        # 若仍走硬编码，cnt_cfg 会与 cnt_base 几乎一致 → 断言失败（RED）
        assert cnt_cfg < cnt_base * 0.5, \
            f"rate_factor 未被 CONFIG 覆盖：阈值调高后 lonely {cnt_cfg}/100 仍接近基线 {cnt_base}/100"


# ── 3. tsundere 0.3/0.5/0.4 可被 CONFIG 覆盖 ──────────────────
def test_tsundere_factors_configurable():
    """tsundere 修饰 0.3/0.5/0.4 必须走 CONFIG"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        # 高 tsundere (90) 会放大 low/mid，抑制 high
        # 基线
        s_base = _make_state(td, now, energy=40, loneliness=45)
        s_base.emotion.tsundere_index = 90
        cnt_base_low = 0
        for i in range(120):
            random.seed(2000+i)
            t = chiguo_trigger.evaluate_triggers(s_base, now)
            if t and t.type == "lonely_low":
                cnt_base_low += 1
        # 将 tsundere 因子全置 0 →修饰消失 → low 触发率应变化
        s_cfg = _make_state(td, now, energy=40, loneliness=45)
        s_cfg.emotion.tsundere_index = 90
        s_cfg.config.setdefault("trigger", {})["lonely_tsundere_low_factor"] = 0.0
        s_cfg.config["trigger"]["lonely_tsundere_mid_factor"] = 0.0
        s_cfg.config["trigger"]["lonely_tsundere_high_factor"] = 0.0
        cnt_cfg_low = 0
        for i in range(120):
            random.seed(2000+i)
            t = chiguo_trigger.evaluate_triggers(s_cfg, now)
            if t and t.type == "lonely_low":
                cnt_cfg_low += 1
        # 若硬编码，cnt_cfg_low == cnt_base_low → 失败
        assert cnt_cfg_low != cnt_base_low, \
            f"tsundere 因子未被 CONFIG 覆盖：置零前后 lonely_low {cnt_base_low} vs {cnt_cfg_low} 完全一致"


# ── 4. w 阈值 0.03 可被 CONFIG 覆盖 ──────────────────────────
def test_w_thresholds_configurable():
    """w_low/w_mid 0.03、w_high 0.02 阈值必须走 CONFIG"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        # 选孤独 30 附近，默认 w_low 刚过 0.03 能触发
        s_base = _make_state(td, now, energy=40, loneliness=30)
        cnt_base = 0
        for i in range(100):
            random.seed(3000+i)
            t = chiguo_trigger.evaluate_triggers(s_base, now)
            if t and t.type.startswith("lonely_"):
                cnt_base += 1
        assert cnt_base > 0, f"基线应有 lonely 触发，实 {cnt_base}/100"

        # 将阈值抬到 0.9 → 全部被过滤 → lonely 应归零
        s_cfg = _make_state(td, now, energy=40, loneliness=30)
        s_cfg.config.setdefault("trigger", {})["lonely_w_low_threshold"] = 0.9
        s_cfg.config["trigger"]["lonely_w_mid_threshold"] = 0.9
        s_cfg.config["trigger"]["lonely_w_high_threshold"] = 0.9
        cnt_cfg = 0
        for i in range(100):
            random.seed(3000+i)
            t = chiguo_trigger.evaluate_triggers(s_cfg, now)
            if t and t.type.startswith("lonely_"):
                cnt_cfg += 1
        assert cnt_cfg == 0, \
            f"w 阈值未被 CONFIG 覆盖：抬到 0.9 后仍触发 {cnt_cfg}/100，基线 {cnt_base}/100"


# ── 5. longing 0.5/0.3/0.03 可被 CONFIG 覆盖 ──────────────────
def test_longing_params_configurable():
    """longing cap 0.5、factor 0.3、min 0.03 必须走 CONFIG"""
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        s_base = _make_state(td, now, energy=40)
        s_base.cooldown.held_count = 5
        s_base.cooldown.accumulated_lambda = 0.5
        cnt_base = 0
        for i in range(80):
            random.seed(4000+i)
            t = chiguo_trigger.evaluate_triggers(s_base, now)
            if t and t.type == "longing":
                cnt_base += 1
        assert cnt_base > 0, f"基线应触发 longing，实 {cnt_base}/80"

        # 将 min_weight 抬到 0.9 → w_longing ~0.3 <0.9 → 不触发
        s_cfg = _make_state(td, now, energy=40)
        s_cfg.cooldown.held_count = 5
        s_cfg.cooldown.accumulated_lambda = 0.5
        s_cfg.config.setdefault("trigger", {})["longing_min_weight"] = 0.9
        cnt_cfg = 0
        for i in range(80):
            random.seed(4000+i)
            t = chiguo_trigger.evaluate_triggers(s_cfg, now)
            if t and t.type == "longing":
                cnt_cfg += 1
        assert cnt_cfg == 0, \
            f"longing min_weight 未被 CONFIG 覆盖：抬到 0.9 后仍 {cnt_cfg}/80，基线 {cnt_base}/80"

        # 额外：cap/factor 覆盖性检查（置 0 应同样归零）
        s_cfg2 = _make_state(td, now, energy=40)
        s_cfg2.cooldown.held_count = 5
        s_cfg2.cooldown.accumulated_lambda = 0.5
        s_cfg2.config.setdefault("trigger", {})["longing_cap"] = 0.0
        s_cfg2.config["trigger"]["longing_factor"] = 0.0
        cnt_cfg2 = 0
        for i in range(80):
            random.seed(4000+i)
            t = chiguo_trigger.evaluate_triggers(s_cfg2, now)
            if t and t.type == "longing":
                cnt_cfg2 += 1
        assert cnt_cfg2 == 0, f"longing cap/factor 未被 CONFIG 覆盖：置零后仍 {cnt_cfg2}/80"


# ── 6. jitter 隔离 Random + CONFIG ───────────────────────────
def test_jitter_uses_isolated_random_and_config():
    """jitter uniform(0.8,1.2) 必须抽为 CONFIG 且用隔离 Random 实例，不污染全局 random"""
    # 检查模块存在隔离实例
    assert hasattr(chiguo_trigger, "_jitter_rng") or hasattr(chiguo_trigger, "_rng") or hasattr(chiguo_trigger, "jitter_rng"), \
        "chiguo_trigger 需暴露隔离的 Random 实例（_jitter_rng / _rng）"
    # 取实例
    rng = getattr(chiguo_trigger, "_jitter_rng", None) or getattr(chiguo_trigger, "_rng", None) or getattr(chiguo_trigger, "jitter_rng", None)
    assert isinstance(rng, random.Random), f"隔离实例应为 random.Random，实为 {type(rng)}"

    # 全局 random 状态不被 jitter 污染：隔离实例消费不影响全局序列
    # 直接验证：全局种子固定后，隔离 RNG 的 uniform 不应改动全局后续序列
    random.seed(12345)
    baseline = [random.random() for _ in range(5)]
    random.seed(12345)
    rng.uniform(0.8, 1.2)
    after_isolated = [random.random() for _ in range(5)]
    assert after_isolated == baseline, \
        f"jitter 隔离失败：隔离 uniform 污染全局 {baseline[:3]} vs {after_isolated[:3]}"
    # 反向对照：全局 uniform 会污染
    random.seed(12345)
    random.uniform(0.8, 1.2)
    after_global = [random.random() for _ in range(5)]
    assert after_global != baseline, "全局 uniform 应污染序列（对照失效）"

    # 额外：evaluate_triggers 整体仍应比未隔离时少消费 1 次全局随机（jitter 已隔离）
    # 基线场景：loneliness=15 energy=40 → 无情绪候选 + 无仪式 → evaluate 不调 jitter/choice → 全局零消耗
    now = datetime(2026, 6, 15, 14, 0, tzinfo=CST)
    with tempfile.TemporaryDirectory() as td:
        s = _make_state(td, now, energy=40, loneliness=15)
        random.seed(12345)
        baseline2 = [random.random() for _ in range(5)]
        random.seed(12345)
        chiguo_trigger.evaluate_triggers(s, now)
        after2 = [random.random() for _ in range(5)]
        # 此场景本就零消耗，隔离与否均应相等（确保无回归）
        assert after2 == baseline2, f"无候选场景不应污染全局 {baseline2[:3]} vs {after2[:3]}"

    # CONFIG 覆盖：jitter 范围可配
    src = Path("chiguo_proactive.toml").read_text()
    assert "jitter_low" in src and "jitter_high" in src, "toml 缺少 jitter_low/jitter_high 配置键"
    # 在 trigger 中用 cfg_float 读取
    trg_src = Path("chiguo_trigger.py").read_text()
    assert "jitter_low" in trg_src and "jitter_high" in trg_src, "trigger 未读取 jitter_low/jitter_high"
    assert "cfg_float" in trg_src, "trigger 应使用 cfg_float 读取 jitter 配置"


# ── 7. toml 缺失 key 已补齐且 cfg_float 读取 ─────────────────
def test_toml_has_new_keys_and_trigger_uses_cfg_float():
    """新增 CONFIG 键必须存在于 toml（主段或 [experimental] 归档）且 trigger 用 cfg_float/_clamp01 读取"""
    cfg = tomllib.loads(Path("chiguo_proactive.toml").read_text(encoding="utf-8"))
    # PR-4 起部分键归档至 [experimental] trigger__*，合并后再检查
    exp = cfg.get("experimental", {}) or {}
    merged_trg = dict(cfg.get("trigger", {}))
    for k, v in exp.items():
        if k.startswith("trigger__"):
            merged_trg[k[len("trigger__"):]] = v
    trg = merged_trg
    required = [
        "lonely_rate_lo_threshold", "lonely_rate_lo_factor",
        "lonely_rate_anx_threshold", "lonely_rate_anx_factor",
        "lonely_tsundere_low_factor", "lonely_tsundere_mid_factor",
        "lonely_tsundere_high_factor", "lonely_bias",
        "lonely_w_low_threshold", "lonely_w_mid_threshold", "lonely_w_high_threshold",
        "longing_cap", "longing_factor", "longing_min_weight",
        "jitter_low", "jitter_high",
    ]
    for k in required:
        assert k in trg, f"toml 缺少 [trigger].{k}（含 experimental 归档）"

    trg_src = Path("chiguo_trigger.py").read_text()
    for k in required:
        assert k in trg_src, f"trigger 未引用 CONFIG 键 {k}"
    # 至少部分键用 cfg_float/_clamp01 读取（非硬编码）
    assert trg_src.count("cfg_float") >= 5, "trigger 中 cfg_float 调用过少，未统一 cfg_float 单源"
    assert "_clamp01" in trg_src, "trigger 应使用 _clamp01 钳制阈值"
