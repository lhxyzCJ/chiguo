#!/usr/bin/env bash
# 全量测试链（35 py + 10 script）——本地与 CI 同一入口；任一失败即退出非零
# 前置: .venv 存在（本地 dev 机已有；CI 由 uv sync 创建）
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

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

# CI fixture：tests/test_integration.py 的 test_7 注入仓库真实课表 data/xskb.xlsx
# （本地课表文件，gitignore 不入库）→ 干净 checkout 由 ci-test.sh 自举最小课表。
# 需解析成功（周一第 1 节，semester_start=2026-02-23 覆盖第 16 周）；data/ 不入库无污染。
if [ ! -f data/xskb.xlsx ]; then
  mkdir -p data
  uv run python - <<'PY'
from openpyxl import Workbook
wb = Workbook()
ws = wb.active
ws.cell(row=5, column=1, value=1)
ws.cell(row=5, column=2, value="高等数学BII(理论)-刘洋【2-17周】尚行楼")
wb.save("data/xskb.xlsx")
PY
fi

if ! (
node tests/test_pi_run.mjs && node tests/test_bridge_askpi.mjs && \
node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && \
node tests/test_bridge_schedule.mjs && bash tests/test_install_pi.sh && \
bash tests/test_wechat_bridge.sh && bash tests/test_netease_api.sh && \
bash tests/test_tick_health.sh && bash tests/test_service.sh && \
uv run python tests/test_chiguo_math.py && uv run python tests/test_holiday_parser.py && \
uv run python tests/test_schedule_parser.py && \
uv run python tests/test_integration.py && uv run python tests/test_monitor.py && \
uv run python tests/test_personality.py && \
uv run python tests/test_bayesian.py && uv run python tests/test_composer.py && \
uv run python tests/test_ebbinghaus.py && uv run python tests/test_longing.py && \
uv run python tests/test_escape_valve.py && uv run python tests/test_feedback.py && \
uv run python tests/test_trigger.py && uv run python tests/test_topics.py && \
uv run python tests/test_circadian.py && uv run python tests/test_followup.py && \
uv run python tests/test_netease_proof.py && uv run python tests/test_netease_service.py && \
uv run python tests/test_envcheck.py && uv run python tests/test_composer_trade.py && \
uv run python tests/test_personality_init.py && uv run python tests/test_toml_binding.py && \
uv run python tests/test_adapt_personality.py && uv run python tests/test_pi_health.py && \
uv run python tests/test_anniversary.py && uv run python tests/test_schedule_override.py && \
uv run python tests/test_day_plan.py && uv run python tests/test_recall.py && \
uv run python tests/test_attention_tiers.py && uv run python tests/test_availability.py && \
uv run python tests/test_trigger_scale.py && uv run python tests/test_isolation.py && \
uv run python tests/test_schedule_plan.py && uv run python tests/test_schedule_cli.py && \
uv run python tests/test_docs_sync.py
); then
  echo "TEST FAILED" >&2
  exit 1
fi

echo "ALL TESTS PASSED"
