#!/usr/bin/env python3
"""test_main_toml_binding.py — 主配置 chiguo_proactive.toml 绑定测试（Issue #238）

M-8 改名（test_toml_binding → test_personality_toml_binding）暴露的缺口：
主 toml 22 节此前无任何专门测试守护。本 pytest 测试覆盖：
1. 22 个顶层节全部存在（架构契约，防节误删/误改名）
2. 每节关键键存在（抽查核心配置键，防键误删）
3. 关键键 ↔ 代码引用点交叉断言（toml 有键且代码有引用，防键改代码没跟上）
"""
import pathlib, tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def check(name, cond, detail=""):
    """断言式 check：失败即抛 AssertionError（pytest 感知为失败）。"""
    if not cond:
        raise AssertionError(f"{name} {detail}")


# 1) 22 节存在（架构契约）
SECTIONS = ["wechat", "memory", "character", "emotion", "sigmoid", "trigger",
            "poisson", "topic_picker", "schedule", "circadian", "netease",
            "hawkes", "cooldown", "personality", "bayesian", "composer",
            "safety", "monitor", "logging", "host", "loop", "health"]


def test_section_inventory_complete():
    """主 toml 22 节全部存在；PR-4 起新增 [experimental] 归档节（61 键灰度归档），
    验收时排除该节（行为恒等：decision/base._merge_experimental 合并回主段）。"""
    cfg = tomllib.loads((ROOT / "chiguo_proactive.toml").read_text(encoding="utf-8"))
    check("主 toml 22 节全部存在", set(SECTIONS) <= set(cfg.keys()),
          f"缺: {sorted(set(SECTIONS) - set(cfg.keys()))}")
    extra = set(cfg.keys()) - set(SECTIONS) - {"experimental"}
    check("主 toml 无多余节（除 [experimental] 归档外）", not extra,
          f"多余: {sorted(extra)}")


# 2) 每节关键键存在（抽查核心配置，防误删）
KEY_CHECKS = {
    "wechat": ["wechat_recipient"],
    "memory": ["backend", "mem0_user_id", "mem0_qdrant_path", "mem0_history_db"],
    "character": ["name", "age"],
    "emotion": ["event_delta_enabled", "anxiety_warmth_recovery",
                "energy_warmth_factor", "impact_inertia_positive"],
    "sigmoid": ["loneliness_high_mid", "anxiety_mid"],
    "trigger": ["min_activation", "must_send_activation",
                "reply_feedback_enabled", "user_mood_ttl_minutes",
                # Q25 收敛 cfg_float 读取键（空闲乘数 / follow_up / 回复率反馈）
                "free_multiplier", "follow_up_weight",
                "reply_feedback_damp", "reply_feedback_boost",
                "reply_feedback_low_rate", "reply_feedback_high_rate",
                "ritual_special_weight", "ritual_mem0_weight",
                "morning_probability", "night_probability", "meal_probability",
                "mem0_surface_min_silent_hours", "mem0_surface_probability",
                "followup_memory_probability", "habit_probability",
                "playful_base_weight", "reflect_base_weight", "reflect_probability"],
    "poisson": ["base_lambda"],
    "topic_picker": ["netease_weight", "netease_daily_quota",
                     "repeat_jaccard_threshold"],
    "schedule": ["enabled", "xlsx_path", "semester_start"],
    "circadian": ["history_days", "min_sample_days"],
    "netease": ["enabled", "play_cache_ttl_minutes", "retry_count",
                # Q25 收敛 cfg_float 读取键（发报退避/重探间隔）
                "retry_backoff_seconds", "reprobe_minutes"],
    "hawkes": ["enabled", "alpha", "beta"],
    "cooldown": ["max_daily_active", "max_daily_silent", "min_interval_minutes",
                 "longing_break_enabled", "drop_damp_window_minutes",
                 # Q25 收敛 cfg_float 读取键（仪式触发权重缩放）
                 "ritual_weight_scale"],
    "personality": ["extraversion", "tsundere_intensity", "regress_rate"],
    "bayesian": ["transition_enabled", "info_gain_threshold"],
    "composer": ["cue_tsundere_weight", "cue_trade_weight",
                 # Q25 收敛 cfg_float 读取键（尺寸权重 + cue 基础权重）
                 "size_1_weight", "size_2_weight", "size_3_weight",
                 "cue_tsundere_soft_weight", "cue_tsundere_cool_weight",
                 "cue_dere_weight", "cue_playful_weight",
                 "cue_anxious_weight", "cue_caring_weight"],
    "safety": ["enabled", "crash_max_in_window"],
    "monitor": ["proactive_eval", "replied_within_hours"],
    "logging": ["retention_months"],
    "host": ["provider", "send_session_id"],
    "loop": ["retry_delay_seconds", "probe_interval_seconds"],
    "health": ["fail_threshold"],
}


def test_section_key_checks_present():
    """每节关键键存在（抽查核心配置键，防键误删）。
    PR-4 起部分键归档至 [experimental]（section__key 形态），验收时经
    decision/base._merge_experimental 合并语义回填后再检查。"""
    cfg = tomllib.loads((ROOT / "chiguo_proactive.toml").read_text(encoding="utf-8"))
    # 模拟 _merge_experimental：把 experimental 的 section__key 合并回主段
    exp = cfg.get("experimental", {}) or {}
    for k, v in exp.items():
        if "__" in k:
            sec, key = k.split("__", 1)
            if sec and key:
                cfg.setdefault(sec, {})[key] = v
    for sec, keys in KEY_CHECKS.items():
        missing = [k for k in keys if k not in cfg.get(sec, {})]
        check(f"[{sec}] 关键键齐全", not missing, f"缺: {missing}")


# 3) 关键键 ↔ 代码引用交叉断言（防键改代码没跟上）
REF_CHECKS = [
    ("[loop].retry_delay_seconds", "runner/loop.py", "retry_delay_seconds"),
    ("[loop].probe_interval_seconds", "runner/loop.py", "probe_interval_seconds"),
    ("[health].fail_threshold", "scripts/agent_health.py", "fail_threshold"),
    ("[wechat].wechat_recipient", "scripts/chiguo-tick.sh", "wechat_recipient"),
    ("[memory].mem0_qdrant_path", "memory/factory.py", "mem0_qdrant_path"),
    ("[cooldown].longing_break_enabled", "state/interaction.py", "longing_break_enabled"),
    ("[trigger].reply_feedback_enabled", "chiguo_trigger.py", "reply_feedback_enabled"),
    ("[trigger].ritual_special_weight", "chiguo_trigger.py", "ritual_special_weight"),
    ("[trigger].mem0_surface_probability", "chiguo_trigger.py", "mem0_surface_probability"),
    ("[trigger].morning_probability", "chiguo_trigger.py", "morning_probability"),
    ("[netease].play_cache_ttl_minutes", "netease/service.py", "play_cache_ttl_minutes"),
    # ── Q25 收敛 cfg_float 读取键 ↔ 代码引用交叉守护 ──
    ("[netease].retry_backoff_seconds", "netease/service.py", "retry_backoff_seconds"),
    ("[netease].reprobe_minutes", "netease/service.py", "reprobe_minutes"),
    ("[cooldown].ritual_weight_scale", "chiguo_trigger.py", "ritual_weight_scale"),
    ("[poisson].base_lambda", "chiguo_trigger.py", "base_lambda"),
    ("[trigger].free_multiplier", "chiguo_trigger.py", "free_multiplier"),
    ("[trigger].follow_up_weight", "chiguo_trigger.py", "follow_up_weight"),
    ("[composer].size_1_weight", "chiguo_composer.py", "size_1_weight"),
    ("[composer].cue_tsundere_weight", "chiguo_composer.py", "cue_tsundere_weight"),
]


def test_key_references_in_code():
    """关键键 ↔ 代码引用点交叉断言（toml 有键且代码有引用，防键改代码没跟上）"""
    for label, code_file, key in REF_CHECKS:
        code = (ROOT / code_file).read_text(encoding="utf-8", errors="replace")
        check(f"{label} 被 {code_file} 引用", key in code,
              f"{code_file} 无 {key} 引用")
