#!/usr/bin/env bash
# 全量测试链——本地与 CI 同一入口；任一失败即退出非零。
# 测试计数不硬编码：启动时动态扫描磁盘 tests/test_*（Q12/#262：杜绝与磁盘实际数目的魔数漂移）。
# 前置: .venv 存在（本地 dev 机已有；CI 由 uv sync 创建）
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# 磁盘测试计数动态计算（Q12/#262）：只作自述/确认，执行仍走下方显式测试命令链。
# py 与 script(含 .mjs/.sh) 分开统计，与 test_docs_sync 磁盘集合口径一致(ID 只取 test_*，fixture 不计)。
py_count=$(find tests -maxdepth 1 -name 'test_*.py' | wc -l)
script_count=$(find tests -maxdepth 1 \( -name 'test_*.mjs' -o -name 'test_*.sh' \) | wc -l)
total_count=$((py_count + script_count))
echo "[ci-test] 磁盘测试文件 ${total_count} 个（${py_count} py + ${script_count} script）"

# CI stub：wechat-bridge 的 @wechatbot/wechatbot 是 file: 本地依赖（仓库外 wechatbot/ 目录），
# 干净 checkout 不存在；bridge.mjs 顶层 import 它但测试从不实例化 → 用最小替身自举。
# 本地已部署真 SDK（wechat-bridge/node_modules）时跳过。
if [ ! -f wechat-bridge/node_modules/@wechatbot/wechatbot/package.json ]; then
  mkdir -p wechat-bridge/node_modules/@wechatbot/wechatbot
  cat > wechat-bridge/node_modules/@wechatbot/wechatbot/package.json <<'EOF'
{"name": "@wechatbot/wechatbot", "version": "0.0.0-ci-stub", "type": "module", "exports": {".": "./index.mjs"}}
EOF
  cat > wechat-bridge/node_modules/@wechatbot/wechatbot/index.mjs <<'EOF'
export class WeChatBot { constructor() {} }
EOF
fi

if ! (
node tests/test_agent_run.mjs && node tests/test_agent_rpc.mjs && node tests/test_bridge_agent_http.mjs && node tests/test_bridge_auth.mjs && node tests/test_bridge_askagent_rpc.mjs && node tests/test_bridge_askagent.mjs && \
node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && \
node tests/test_bridge_rotate.mjs && node tests/test_bridge_schedule.mjs && bash tests/test_install_agent.sh && \
bash tests/test_wechat_bridge.sh && bash tests/test_netease_api.sh && \
bash tests/test_tick_health.sh && bash tests/test_service.sh && \
uv run python tests/test_chiguo_math.py && uv run python tests/test_config_util.py && \
uv run python tests/test_emotion_dynamics.py && \
uv run python tests/test_emotion_noise.py && uv run python tests/test_emotion_baseline.py && uv run python tests/test_loop_send.py && uv run python tests/test_loop_concurrency.py && uv run python tests/test_form_guard.py && \
uv run python tests/test_holiday_parser.py && \
uv run python tests/test_schedule_parser.py && \
uv run python tests/test_integration.py && uv run python tests/test_monitor.py && \
uv run python tests/test_personality.py && \
uv run python tests/test_bayesian.py && uv run python tests/test_composer.py && \
uv run python tests/test_composer_fallback.py && \
uv run python tests/test_ebbinghaus.py && uv run python tests/test_longing.py && \
uv run python tests/test_escape_valve.py && uv run python tests/test_feedback.py && \
uv run python tests/test_impact_inertia.py && uv run python tests/test_user_mood.py && \
uv run python tests/test_trigger.py && uv run python tests/test_topics.py && \
uv run python tests/test_circadian.py && uv run python tests/test_followup.py && \
uv run python tests/test_state_sleep.py && uv run python tests/test_state_migrations.py && uv run python tests/test_monitor_hardening.py && \
uv run python tests/test_demo.py && uv run python tests/test_daemon_fixes.py && \
uv run python tests/test_decision_schema.py && \
uv run python tests/test_reminder_dedup_persist.py && \
uv run python tests/test_netease_proof.py && uv run python tests/test_netease_service.py && \
uv run python tests/test_envcheck.py && uv run python tests/test_composer_trade.py && \
uv run python tests/test_personality_init.py && uv run python tests/test_personality_toml_binding.py && \
uv run python tests/test_adapt_personality.py && uv run python tests/test_agent_health.py && \
uv run python tests/test_anniversary.py && uv run python tests/test_schedule_override.py && \
uv run python tests/test_day_plan.py && uv run python tests/test_recall.py && \
uv run python tests/test_attention_tiers.py && uv run python tests/test_availability.py && \
uv run python tests/test_trigger_scale.py && uv run python tests/test_trigger_types.py && \
uv run python tests/test_isolation.py && uv run python tests/test_state_private_access_guard.py && \
uv run python tests/test_schedule_plan.py && uv run python tests/test_schedule_cli.py && \
uv run python tests/test_memory_backends.py && \
uv run python tests/test_memory_consolidate.py && \
uv run python tests/test_memory_reinforce.py && \
uv run python tests/test_metadata_cleanup.py && \
uv run python tests/test_full_turns.py && \
uv run python tests/test_proactive_eval.py && \
uv run python tests/test_bayesian_transition.py && \
uv run python tests/test_info_gain.py && \
uv run python tests/test_reply_feedback.py && \
uv run python tests/test_event_delta.py && \
uv run python tests/test_emotion_tagging.py && \
uv run python tests/test_consolidate_cli.py && \
uv run python tests/test_chiguo_version.py && \
uv run python tests/test_daemon_cli_snapshot.py && \
uv run python tests/test_main_toml_binding.py && \
uv run python tests/test_infra_consistency.py && \
uv run python tests/test_docs_sync.py
); then
  echo "TEST FAILED" >&2
  exit 1
fi

echo "ALL TESTS PASSED"
