#!/usr/bin/env bash
# 全量测试链（py 由 pytest 动态收集 + mjs/sh 脚本链保留）——本地与 CI 同一入口；任一失败即退出非零
# Q26 迁移：py 测试改 `uv run pytest tests/ -q`（原 61 个逐文件 runner），计数不硬编码、按收集结果动态计算。
# 前置: .venv 存在（本地 dev 机已有；CI 由 uv sync 创建）
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# 磁盘测试计数动态计算（Q12/#262）：只作自述/确认，执行仍走下方显式测试命令链。
# py 与 script(含 .mjs/.sh) 分开统计，与 test_docs_sync 磁盘集合口径一致(ID 只取 test_*，fixture 不计)。
py_count=$(find tests -maxdepth 1 -name 'test_*.py' | wc -l)
script_count=$(find tests -maxdepth 1 \( -name 'test_*.mjs' -o -name 'test_*.sh' \) | wc -l)
total_count=$((py_count + script_count))
echo "[ci-test] 磁盘测试文件 ${total_count} 个（${py_count} py + ${script_count} script）"

# wechat-bridge 的 @wechatbot/wechatbot 依赖随仓库 vendor（wechat-bridge/vendor/wechatbot，MIT，含 LICENSE）。
# 干净 checkout 无已构建产物 → 构建真实 SDK（npm install 依赖 + tsc build），再从桥目录 npm install 建立 file: 链接。
# 任一失败即中止（CI 由此验证 vendor 真实 SDK 可从源码构建）。
if [ ! -f wechat-bridge/vendor/wechatbot/dist/index.js ]; then
  echo "[ci-test] 构建 vendor SDK（wechat-bridge/vendor/wechatbot）..."
  ( cd wechat-bridge/vendor/wechatbot && npm install --no-fund --no-audit && npm run build )
fi
if [ ! -d wechat-bridge/node_modules/@wechatbot ]; then
  echo "[ci-test] 安装桥依赖（npm install file:./vendor/wechatbot）..."
  ( cd wechat-bridge && npm install --no-fund --no-audit )
fi
# B1（#313）：npm install file:./vendor/wechatbot 会把 node_modules/@wechatbot/wechatbot 建成
# 指向 vendor/wechatbot 的软链。tests/test_bridge_auth.mjs 的 ensureStub() 会写该路径——
# 经软链穿透到 git 跟踪的 vendor 源（覆写 package.json + 新增 index.mjs），每次 ci-test 后
# 都需手动 `git checkout -- vendor/wechatbot/package.json && rm vendor/wechatbot/index.mjs`。
# 根治：把软链物化为真实副本（不含 vendor 自身 node_modules，只留 SDK 运行产物），
# 令测试 stub 只落在 gitignored 的 node_modules，不再穿透 vendor。幂等：已是副本则跳过。
if [ -L wechat-bridge/node_modules/@wechatbot/wechatbot ]; then
  echo "[ci-test] 物化 node_modules/@wechatbot/wechatbot 为真实副本（隔离测试 stub，防穿透 vendor）..."
  rm -f wechat-bridge/node_modules/@wechatbot/wechatbot
  mkdir -p wechat-bridge/node_modules/@wechatbot/wechatbot
  ( cd wechat-bridge/vendor/wechatbot && tar cf - --exclude=node_modules . ) \
    | ( cd wechat-bridge/node_modules/@wechatbot/wechatbot && tar xf - )
fi

# 动态计数（不硬编码数量，均按磁盘/pytest 收集结果实时计算）
SCRIPT_MJS=$(ls tests/test_*.mjs 2>/dev/null | wc -l)
SCRIPT_SH=$(ls tests/test_*.sh 2>/dev/null | wc -l)
PY_COL_COUNT=$(uv run pytest tests/ --collect-only -q 2>&1 | grep -oE '[0-9]+ tests collected' | tail -1)
PY_COUNT=$(printf '%s' "$PY_COL_COUNT" | grep -oE '^[0-9]+')

# 1) 脚本链（mjs + sh，Q26 保留非 py 测试链）
if ! (
node tests/test_agent_run.mjs && node tests/test_agent_rpc.mjs && node tests/test_bridge_sdk_vendor.mjs && node tests/test_bridge_agent_http.mjs && node tests/test_bridge_auth.mjs && node tests/test_bridge_askagent_rpc.mjs && node tests/test_bridge_askagent.mjs && \
node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && \
node tests/test_bridge_send_timeout.mjs && \
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
