#!/usr/bin/env python3
"""test_main_toml_binding.py — 主配置 chiguo_proactive.toml 绑定测试（Issue #238）

M-8 改名（test_toml_binding → test_personality_toml_binding）暴露的缺口：
主 toml 22 节此前无任何专门测试守护。本 runner 覆盖：
1. 22 个顶层节全部存在（架构契约，防节误删/误改名）
2. 每节关键键存在（抽查核心配置键，防键误删）
3. 关键键 ↔ 代码引用点交叉断言（toml 有键且代码有引用，防键改代码没跟上）
"""
import pathlib, sys, tomllib

ROOT = pathlib.Path(__file__).resolve().parent.parent
FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok - {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL - {name} {detail}")


cfg = tomllib.loads((ROOT / "chiguo_proactive.toml").read_text(encoding="utf-8"))

# 1) 22 节存在（架构契约）
SECTIONS = ["wechat", "memory", "character", "emotion", "sigmoid", "trigger",
            "poisson", "topic_picker", "schedule", "circadian", "netease",
            "hawkes", "cooldown", "personality", "bayesian", "composer",
            "safety", "monitor", "logging", "host", "loop", "health"]
check("主 toml 22 节全部存在", set(SECTIONS) <= set(cfg.keys()),
      f"缺: {sorted(set(SECTIONS) - set(cfg.keys()))}")
check("主 toml 无多余节", set(cfg.keys()) <= set(SECTIONS),
      f"多余: {sorted(set(cfg.keys()) - set(SECTIONS))}")

# 2) 每节关键键存在（抽查核心配置，防误删）
KEY_CHECKS = {
    "wechat": ["wechat_recipient"],
    "memory": ["backend", "mem0_user_id", "mem0_qdrant_path", "mem0_history_db"],
    "character": ["name", "age"],
    "emotion": ["event_delta_enabled", "anxiety_warmth_recovery",
                "energy_warmth_factor", "impact_inertia_positive"],
    "sigmoid": ["loneliness_high_mid", "anxiety_mid"],
    "trigger": ["min_activation", "must_send_activation",
                "reply_feedback_enabled", "user_mood_ttl_minutes"],
    "poisson": ["base_lambda"],
    "topic_picker": ["netease_weight", "netease_daily_quota",
                     "repeat_jaccard_threshold"],
    "schedule": ["enabled", "xlsx_path", "semester_start"],
    "circadian": ["history_days", "min_sample_days"],
    "netease": ["enabled", "play_cache_ttl_minutes", "retry_count"],
    "hawkes": ["enabled", "alpha", "beta"],
    "cooldown": ["max_daily_active", "max_daily_silent", "min_interval_minutes",
                 "longing_break_enabled", "drop_damp_window_minutes"],
    "personality": ["extraversion", "tsundere_intensity", "regress_rate"],
    "bayesian": ["transition_enabled", "info_gain_threshold"],
    "composer": ["cue_tsundere_weight", "cue_trade_weight"],
    "safety": ["enabled", "crash_max_in_window"],
    "monitor": ["proactive_eval", "replied_within_hours"],
    "logging": ["retention_months"],
    "host": ["provider", "send_session_id"],
    "loop": ["retry_delay_seconds", "probe_interval_seconds"],
    "health": ["fail_threshold"],
}
for sec, keys in KEY_CHECKS.items():
    missing = [k for k in keys if k not in cfg.get(sec, {})]
    check(f"[{sec}] 关键键齐全", not missing, f"缺: {missing}")

# 3) 关键键 ↔ 代码引用交叉断言（防键改代码没跟上）
REF_CHECKS = [
    ("[loop].retry_delay_seconds", "chiguo_daemon.py", "retry_delay_seconds"),
    ("[loop].probe_interval_seconds", "chiguo_daemon.py", "probe_interval_seconds"),
    ("[health].fail_threshold", "scripts/agent_health.py", "fail_threshold"),
    ("[wechat].wechat_recipient", "scripts/chiguo-tick.sh", "wechat_recipient"),
    ("[memory].mem0_qdrant_path", "memory/factory.py", "mem0_qdrant_path"),
    ("[cooldown].longing_break_enabled", "chiguo_state.py", "longing_break_enabled"),
    ("[trigger].reply_feedback_enabled", "chiguo_trigger.py", "reply_feedback_enabled"),
    ("[netease].play_cache_ttl_minutes", "netease/service.py", "play_cache_ttl_minutes"),
]
for label, code_file, key in REF_CHECKS:
    code = (ROOT / code_file).read_text(encoding="utf-8", errors="replace")
    check(f"{label} 被 {code_file} 引用", key in code,
          f"{code_file} 无 {key} 引用")

if FAIL:
    print(f"\n{len(FAIL)} 项失败", file=sys.stderr)
    sys.exit(1)
print("\n全部通过")
