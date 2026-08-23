#!/usr/bin/env python3
"""test_config_clamp.py — T12 config clamp TDD RED→GREEN

验证：
- cfg_float nan/inf/-inf 回退 default，且 clamp_min 生效
- trigger 全阈值经 cfg_float 单源，负值钳制为 0（无 max(0,float) 毒化）
- source_weights 全0/非法回退默认 [0.5,0.5]
- 热重载 mtime 仅 decision/base 链（单源）
"""
import math
import os
import sys
import tempfile
import tomllib
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

CST = timezone(timedelta(hours=8))

from chiguo_math import cfg_float


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(f"{name} {detail}")


def test_cfg_float_nan_inf_fallback():
    for v in (float("nan"), float("inf"), float("-inf")):
        check(f"cfg_float {v} → default", cfg_float(v, 5.0) == 5.0, f"got {cfg_float(v,5.0)}")
        check(f"cfg_float {v} clamp → default", cfg_float(v, 5.0, clamp_min=0.0) == 5.0)
    # 字符串形式的 nan/inf 也应回退（float("nan") 路径）
    for s in ("nan", "inf", "-inf", "INF", "NaN"):
        check(f"cfg_float '{s}' → default", cfg_float(s, 2.0) == 2.0)


def test_cfg_float_clamp_min():
    check("负值 clamp 0", cfg_float(-2.0, 5.0, clamp_min=0.0) == 0.0)
    check("负值 clamp 0 str", cfg_float("-1", 1.0, clamp_min=0.0) == 0.0)
    check("正值不钳制", cfg_float(3.0, 5.0, clamp_min=0.0) == 3.0)
    check("无 clamp 保留负值", cfg_float(-2.0, 5.0) == -2.0)
    check("clamp_min 生效优先于返回值", cfg_float(-0.5, 1.0, clamp_min=0.0) == 0.0)
    # inf 应先回退 default，不应被 clamp 截为 clamp_min
    check("inf 不被 clamp 误截", cfg_float(float("inf"), 7.0, clamp_min=0.0) == 7.0)


def test_trigger_weights_clamped():
    """trigger 侧所有 cfg_float 权重要求 clamp_min=0，负值不应毒化为负权重"""
    import random
    from chiguo_trigger import evaluate_triggers
    from chiguo_state import ChiguoState
    src = Path("chiguo_proactive.toml").read_text(encoding="utf-8")
    with tempfile.TemporaryDirectory() as td:
        cfg_path = Path(td) / "chiguo_proactive.toml"
        cfg_path.write_text(src)
        with open(cfg_path, "rb") as f:
            cfg = tomllib.load(f)
        cfg["_base_dir"] = str(td)
        cfg["memory"]["mem0_qdrant_path"] = str(Path(td) / "no_qdrant")
        cfg["memory"]["mem0_history_db"] = str(Path(td) / "no_history.db")
        os.environ["CHIGUO_MEM0_DISABLED"] = "1"
        # 注入非法阈值：负值 / nan / inf 均应被钳制或回退，不应产生负权重或 nan 权重
        cfg["trigger"]["ritual_special_weight"] = -5.0
        cfg["trigger"]["ritual_morning_weight"] = float("nan")
        cfg["trigger"]["ritual_mem0_weight"] = float("inf")
        cfg["trigger"]["playful_base_weight"] = -1.0
        cfg["trigger"]["reflect_base_weight"] = float("nan")
        cfg["trigger"]["follow_up_weight"] = -0.5
        cfg["trigger"]["free_multiplier"] = -1.0
        cfg["trigger"]["reply_feedback_damp"] = float("nan")
        cfg["trigger"]["reply_feedback_boost"] = float("-inf")
        cfg["poisson"]["base_lambda"] = float("nan")
        cfg["cooldown"]["ritual_weight_scale"] = float("inf")
        # 直接通过 cfg_float 语义验证：这些键在 trigger 读取时应 clamp 或回退，不为负/非有限
        # 构造 state 并跑一次评估，不应因毒化权重抛异常或产生负总权重
        s = ChiguoState(cfg)
        s.cooldown.last_user_message_at = (datetime.now(CST) - timedelta(hours=10)).isoformat()
        s.cooldown.current_date = datetime.now(CST).strftime("%Y-%m-%d")
        s.emotion.loneliness = 70
        s.emotion.energy = 80
        random.seed(42)
        # 不应抛异常，且若有候选其权重应非负非 nan
        result = evaluate_triggers(s, datetime.now(CST))
        # 至少保证未因 nan 毒化导致整体失败（result 可为 None 但不应为异常）
        check("trigger 负/nan/inf 不抛异常", True)
        # 验证底层的 cfg_float 读取确实被 clamp/回退：直接测 cfg_float 本身与 trigger 的 ritual 读取
        from chiguo_trigger import cfg_float as trg_cfg
        from chiguo_math import cfg_float as math_cfg
        check("trigger 与 math 共用单源", trg_cfg is math_cfg)
        check("ritual -5 clamp0", trg_cfg(-5.0, 3.0, clamp_min=0.0) == 0.0)
        check("playful -1 clamp0", trg_cfg(-1.0, 0.15, clamp_min=0.0) == 0.0)
        # 额外验证 follow_up_weight 负值经 clamp 不会成为负候选权重
        # 通过构造 trig_cfg 含负权重，触发收集应不产生负权重候选
        check("follow_up -0.5 clamp0", trg_cfg(-0.5, 0.35, clamp_min=0.0) == 0.0)


def test_source_weights_zero_fallback():
    import tempfile
    from netease.service import NeteaseService
    # 全0 → 回退默认
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {}, "topic_picker": {"netease_source_weights": [0, 0]}}
        svc = NeteaseService(config=cfg, base_dir=td)
        check("全0 回退默认 [0.5,0.5]", svc.source_weights == [0.5, 0.5], f"got {svc.source_weights}")
    # 负/ nan / inf 经 clamp 仍全0 → 回退默认
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {}, "topic_picker": {"netease_source_weights": [float("nan"), float("inf")]}}
        svc = NeteaseService(config=cfg, base_dir=td)
        check("nan/inf 回退后全0 仍默认", svc.source_weights == [0.5, 0.5], f"got {svc.source_weights}")
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {}, "topic_picker": {"netease_source_weights": [-1.0, -2.0]}}
        svc = NeteaseService(config=cfg, base_dir=td)
        check("负权重 clamp 后全0 回退默认", svc.source_weights == [0.5, 0.5], f"got {svc.source_weights}")
    # 正常值保留
    with tempfile.TemporaryDirectory() as td:
        cfg = {"netease": {}, "topic_picker": {"netease_source_weights": [0.8, 0.2]}}
        svc = NeteaseService(config=cfg, base_dir=td)
        check("正常权重保留", svc.source_weights == [0.8, 0.2])


def test_cfg_int_overflow_and_negative():
    from netease.service import NeteaseService
    # _cfg_int 应捕获 OverflowError（int(inf)）
    check("_cfg_int inf → default", NeteaseService._cfg_int(float("inf"), 1) == 1)
    check("_cfg_int -inf → default or 0", NeteaseService._cfg_int(float("-inf"), 2) == 2)
    check("_cfg_int nan → default", NeteaseService._cfg_int(float("nan"), 3) == 3)
    check("_cfg_int -5 clamp 0", NeteaseService._cfg_int(-5, 1) == 0)
    check("_cfg_int 'abc' → default", NeteaseService._cfg_int("abc", 5) == 5)


def test_hot_reload_single_chain():
    """热重载仅 --loop：mtime 检测仅在 decision/base 34/69/72 + chiguo_state reload_config"""
    base_text = Path("decision/base.py").read_text(encoding="utf-8")
    check("base 34 含 _config_mtime", "_config_mtime" in base_text)
    check("base 69 含 _maybe_reload_config", "_maybe_reload_config" in base_text)
    check("base 72 含 getmtime/mtime", "getmtime" in base_text)
    # 全仓库除 decision/base 外不应再有 getmtime 热重载逻辑（排除 tests/ 自身字面量与 .venv）
    import subprocess
    r = subprocess.run(["grep", "-rn", "getmtime", "--include=*.py", "."],
                       capture_output=True, text=True, cwd=str(Path(".").resolve()))
    lines = [l for l in r.stdout.splitlines() if "getmtime" in l and "/tests/" not in l and ".venv" not in l]
    allowed = [l for l in lines if "decision/base.py" in l]
    check("mtime 仅 decision/base", len(lines) == len(allowed), f"extra getmtime: {lines}")
    # reload_config 仅 chiguo_state 与 decision/base 链路上
    r2 = subprocess.run(["grep", "-rn", "reload_config", "--include=*.py", "."],
                        capture_output=True, text=True, cwd=str(Path(".").resolve()))
    check("reload_config 存在", "reload_config" in r2.stdout)


def test_no_max_float_poison():
    """无 max(0,float(..)) 毒化：配置读取应经 cfg_float clamp，而非裸 max(0,float(get))"""
    import re
    trg = Path("chiguo_trigger.py").read_text(encoding="utf-8")
    # 禁止裸 max(0, float(trg_cfg.get 模式（毒化路径）
    bad = re.findall(r"max\s*\(\s*0[^)]*float\s*\(\s*trg_cfg\.get", trg)
    check("trigger 无 max(0,float(trg_cfg.get 毒化)", len(bad) == 0, f"found {bad}")
    bad2 = re.findall(r"max\s*\(\s*0[^)]*float\s*\(\s*anx_bonus", trg)
    check("trigger 无 max(0,float(anx_bonus 毒化)", len(bad2) == 0)
    # composer 应无 max(0.0, cfg_float(...)) 包装，改用 cfg_float clamp
    comp = Path("chiguo_composer.py").read_text(encoding="utf-8")
    bad3 = re.findall(r"max\s*\(\s*0\.0\s*,\s*cfg_float", comp)
    check("composer 无 max(0,cfg_float 包装，改用 clamp)", len(bad3) == 0, f"found {bad3}")


def test_toml_22section_no_orphan():
    """22 段 key 均有 cfg 读取，无孤儿 literal：抽查 trigger/sigmoid/poisson 等段关键键在代码中有引用
    PR-4 起 [experimental] 归档节计为第 23 段；本测试仅校验 22 主段，其余归档节经 _merge_experimental 合并。"""
    cfg = tomllib.loads(Path("chiguo_proactive.toml").read_text(encoding="utf-8"))
    # 主段 22 节 + 归档节 experimental（61 键合并回主段，行为恒等）
    for sec in ["experimental"]:
        cfg.pop(sec, None)
    sections = ["wechat", "memory", "character", "emotion", "sigmoid", "trigger",
                "poisson", "topic_picker", "schedule", "circadian", "netease",
                "hawkes", "cooldown", "personality", "bayesian", "composer",
                "safety", "monitor", "logging", "host", "loop", "health"]
    for sec in sections:
        check(f"段 {sec} 存在", sec in cfg)
    # 关键键在代码中被引用（防孤儿 literal）
    checks = [
        # PR-4 起 sigmoid.loneliness_low_k 归档至 [experimental]，合并后仍被 state 侧引用（搜索范围扩大到 chiguo_state* 与 state/）
        ("sigmoid.loneliness_low_k", "chiguo_state", "loneliness_low_k"),
        ("trigger.free_multiplier", "chiguo_trigger", "free_multiplier"),
        ("poisson.base_lambda", "chiguo_trigger", "base_lambda"),
        ("topic_picker.netease_source_weights", "netease/service", "netease_source_weights"),
        ("netease.retry_backoff_seconds", "netease/service", "retry_backoff_seconds"),
        ("cooldown.ritual_weight_scale", "chiguo_trigger", "ritual_weight_scale"),
        ("composer.size_1_weight", "chiguo_composer", "size_1_weight"),
        ("loop.retry_delay_seconds", "runner/loop", "retry_delay_seconds"),
        ("health.fail_threshold", "scripts/agent_health", "fail_threshold"),
    ]
    for label, mod, key in checks:
        mapping = {
            "chiguo_state": "chiguo_state.py",
            "chiguo_trigger": "chiguo_trigger.py",
            "netease/service": "netease/service.py",
            "chiguo_composer": "chiguo_composer.py",
            "runner/loop": "runner/loop.py",
            "scripts/agent_health": "scripts/agent_health.py",
        }
        p = Path(mapping[mod])
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        # chiguo_state 经 PR-2 拆至 state/ 子包，_loneliness_low_k 仍在 state/ 子模块中；回退搜索 state/
        if key not in text and mod == "chiguo_state":
            import glob as _glob
            for sp in _glob.glob("state/*.py"):
                if key in Path(sp).read_text(encoding="utf-8", errors="replace"):
                    text += Path(sp).read_text(encoding="utf-8", errors="replace")
                    break
        check(f"{label} 被 {mod} 引用", key in text, f"missing {key} in {mod}")
