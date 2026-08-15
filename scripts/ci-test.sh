#!/usr/bin/env bash
# 全量测试链（py 由 pytest 动态收集 + mjs/sh 脚本链保留）——本地与 CI 同一入口；任一失败即退出非零
# Q26 迁移：py 测试改 `uv run pytest tests/ -q`（原 61 个逐文件 runner），计数不硬编码、按收集结果动态计算。
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

# 动态计数（不硬编码数量，均按磁盘/pytest 收集结果实时计算）
SCRIPT_MJS=$(ls tests/test_*.mjs 2>/dev/null | wc -l)
SCRIPT_SH=$(ls tests/test_*.sh 2>/dev/null | wc -l)
PY_COL_COUNT=$(uv run pytest tests/ --collect-only -q 2>&1 | grep -oE '[0-9]+ tests collected' | tail -1)
PY_COUNT=$(printf '%s' "$PY_COL_COUNT" | grep -oE '^[0-9]+')

# 1) 脚本链（mjs + sh，Q26 保留非 py 测试链）
if ! (
node tests/test_agent_run.mjs && node tests/test_agent_rpc.mjs && node tests/test_bridge_agent_http.mjs && node tests/test_bridge_askagent_rpc.mjs && node tests/test_bridge_askagent.mjs && \
node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && \
node tests/test_bridge_rotate.mjs && node tests/test_bridge_schedule.mjs && bash tests/test_install_agent.sh && \
bash tests/test_wechat_bridge.sh && bash tests/test_netease_api.sh && \
bash tests/test_tick_health.sh && bash tests/test_service.sh
); then
  echo "SCRIPT TEST FAILED" >&2
  exit 1
fi

# 2) pytest 全量 py 测试
if ! uv run pytest tests/ -q; then
  echo "PYTEST FAILED" >&2
  exit 1
fi

echo "ALL TESTS PASSED (pytest ${PY_COUNT} py + ${SCRIPT_MJS} mjs + ${SCRIPT_SH} sh)"
