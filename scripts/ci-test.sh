#!/usr/bin/env bash
# 全量测试链（py 由 pytest 动态收集 + mjs/sh 脚本链保留）——本地与 CI 同一入口；任一失败即退出非零
# Q26 迁移：py 测试改 `uv run pytest tests/ -q`（原 61 个逐文件 runner），计数不硬编码、按收集结果动态计算。
# 前置: .venv 存在（本地 dev 机已有；CI 由 uv sync 创建）
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

# 磁盘测试计数动态计算（Q12/#262）：只作自述/确认，执行仍走下方显式测试命令链。
py_count=$(find tests -maxdepth 1 -name 'test_*.py' | wc -l)
script_count=$(find tests -maxdepth 1 \( -name 'test_*.mjs' -o -name 'test_*.sh' \) | wc -l)
total_count=$((py_count + script_count))
echo "[ci-test] 磁盘测试文件 ${total_count} 个（${py_count} py + ${script_count} script）"

# wechat-bridge 的 @wechatbot/wechatbot 依赖随仓库 vendor（wechat-bridge/vendor/wechatbot，MIT，含 LICENSE）。
# 干净 checkout 无已构建产物 → 构建真实 SDK（npm ci 确定性安装 + tsc build），再从桥目录 npm ci 建立 file: 链接。
# package-lock.json 已跟踪入库，npm ci 可用；CI 只缓存 ~/.npm + dist，不缓存 node_modules（防陈旧树触发 arborist edgesOut）。
if [ ! -f wechat-bridge/vendor/wechatbot/dist/index.js ]; then
  echo "[ci-test] 构建 vendor SDK（wechat-bridge/vendor/wechatbot）..."
  ( cd wechat-bridge/vendor/wechatbot && npm ci --no-fund --no-audit && npm run build )
else
  if [ -n "$(find wechat-bridge/vendor/wechatbot/src -newer wechat-bridge/vendor/wechatbot/dist/index.js 2>/dev/null | head -n 1)" ] \
     || [ wechat-bridge/vendor/wechatbot/tsconfig.json -nt wechat-bridge/vendor/wechatbot/dist/index.js ] \
     || [ wechat-bridge/vendor/wechatbot/package.json -nt wechat-bridge/vendor/wechatbot/dist/index.js ]; then
    echo "[ci-test] vendor 源码已更新，重编 dist..."
    # 缓存仅恢复 dist 不恢复 node_modules（防 arborist edgesOut），重编前须先 npm ci，
    # 否则 tsc 缺 @types/node 报 TS2591（2026-09-05 main 全红根因）。
    ( cd wechat-bridge/vendor/wechatbot && npm ci --no-fund --no-audit && npm run build )
  fi
fi
if [ ! -d wechat-bridge/node_modules/@wechatbot ]; then
  echo "[ci-test] 安装桥依赖（npm ci，解析 package.json 内 file: 依赖）..."
  ( cd wechat-bridge && npm ci --no-fund --no-audit )
fi
# B1（#313）：npm ci file:./vendor/wechatbot 会把 node_modules/@wechatbot/wechatbot 建成
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

# 1) 脚本链（mjs + sh，Q26 保留非 py 测试链）— 并行化（3 组，schedule 单测 120s 空窗被并行掩盖 → 131s -> ~60s）
_script_fail=0
(
  node tests/test_home_dir.mjs && node tests/test_agent_run.mjs && node tests/test_agent_rpc.mjs && node tests/test_bridge_sdk_vendor.mjs && node tests/test_bridge_agent_http.mjs && node tests/test_bridge_auth.mjs
) & _pid_a=$!
(
  node tests/test_bridge_askagent_rpc.mjs && node tests/test_bridge_askagent.mjs && node tests/test_bridge_cmd.mjs && node tests/test_bridge_health.mjs && node tests/test_bridge_send_timeout.mjs && node tests/test_bridge_rotate.mjs
) & _pid_b=$!
(
  _inner_fail=0
  node tests/test_bridge_schedule.mjs & _pid_s=$!
  bash tests/test_install_agent.sh & _pid_c1=$!
  bash tests/test_wechat_bridge.sh & _pid_c2=$!
  bash tests/test_netease_api.sh & _pid_c3=$!
  wait "$_pid_s" || _inner_fail=1
  wait "$_pid_c1" || _inner_fail=1
  wait "$_pid_c2" || _inner_fail=1
  wait "$_pid_c3" || _inner_fail=1
  exit "$_inner_fail"
) & _pid_c=$!
wait "$_pid_a" || _script_fail=1
wait "$_pid_b" || _script_fail=1
wait "$_pid_c" || _script_fail=1
# 剩余两条重型 sh（依赖前组产物/端口隔离，单独串行收尾）
if [ "$_script_fail" -eq 0 ]; then
  bash tests/test_tick_health.sh || _script_fail=1
  bash tests/test_service.sh || _script_fail=1
fi
if [ "$_script_fail" -ne 0 ]; then
  echo "SCRIPT TEST FAILED" >&2
  exit 1
fi

# 2) pytest 全量 py 测试（并行化：xdist -n auto；小集合走单进程避免 worker 开销）
if uv run python -c "import xdist" 2>/dev/null && [ "${PY_COUNT:-0}" -ge 50 ] 2>/dev/null; then
  _pytest_args="-n auto"
else
  _pytest_args=""
fi
if ! uv run pytest tests/ -q $_pytest_args; then
  echo "PYTEST FAILED" >&2
  exit 1
fi

echo "ALL TESTS PASSED (pytest ${PY_COUNT} py + ${SCRIPT_MJS} mjs + ${SCRIPT_SH} sh)"
