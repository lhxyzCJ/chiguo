#!/usr/bin/env bash
# ============================================================
# wechat-bridge.sh — 迟菓微信桥（wechatbot iLink SDK）管理脚本
# 可移植：任意机器 pull chiguo 仓库后即可安装启动。
# 用法: bash scripts/wechat-bridge.sh <install|start|stop|status|login>
# 退出码: 0=OK  1=可恢复问题（如未登录）  2=严重问题
# 幂等：install/start 重复运行安全。
# ============================================================
set -uo pipefail

PROJECT_DIR="${CHIGUO_REPO_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRIDGE_DIR="$PROJECT_DIR/wechat-bridge"
ENV_FILE="$BRIDGE_DIR/.env"
LOG_FILE="${WECHAT_BRIDGE_LOG:-/tmp/opencode/wechat-bridge.log}"
WECHATBOT_DIR="${WECHATBOT_DIR:-$HOME/wechatbot}"
WECHATBOT_REPO="${WECHATBOT_REPO:-https://github.com/lhxyzCJ/wechatbot.git}"
SEND_PORT="${WECHAT_BRIDGE_SEND_PORT:-18790}"
# 集中认证目录（可迁移：拷贝 ~/.chiguo/auth/ 到新机器即可接入；微信登录态失效会自动重登）
AUTH_DIR="${CHIGUO_AUTH_DIR:-$HOME/.chiguo/auth}"
WX_STORAGE="$AUTH_DIR/wechat"
# 收件人解析链：登录后的 credentials.json userId（真实）→ toml wechat_recipient（用户手配）→ 占位符
resolve_owner() {
  local uid toml_owner
  uid="$(sed -n 's/.*"userId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WX_STORAGE/credentials.json" 2>/dev/null | head -1 || true)"
  [ -n "$uid" ] && { echo "$uid"; return; }
  toml_owner="$(sed -n 's/^wechat_recipient *= *"\(.*\)"/\1/p' "$PROJECT_DIR/chiguo_proactive.toml" | head -1)"
  if [ -n "$toml_owner" ] && [ "$toml_owner" != "owner@im.wechat" ]; then echo "$toml_owner"; return; fi
  echo "owner@im.wechat"
}
OWNER_ID="$(resolve_owner)"

say() { printf '\033[1;32m[wechat-bridge]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[wechat-bridge]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[wechat-bridge]\033[0m %s\n' "$*"; exit 2; }

has_credentials() { [ -f "$WX_STORAGE/credentials.json" ]; }

write_env() {
    mkdir -p "$BRIDGE_DIR"
    # memory-lancedb-pro smart extraction 的 key：~/.pi/agent/auth.json——优先 opencode-go 条目
    # （扩展 json5 llm 端点固定 opencode 网关），无则回退 [host].provider 条目（install_agent.sh 写入）
    AGENT_FALLBACK_PROVIDER="$(sed -n 's/^[[:space:]]*provider *= *"\([^"]*\)".*/\1/p' "$PROJECT_DIR/chiguo_proactive.toml" | head -1 || true)"
    [ -n "$AGENT_FALLBACK_PROVIDER" ] || AGENT_FALLBACK_PROVIDER=opencode-go
    AGENT_KEY="$(AGENT_FALLBACK_PROVIDER="$AGENT_FALLBACK_PROVIDER" python3 -c "
import json,os
try:
    d=json.load(open(os.path.expanduser('~/.pi/agent/auth.json')))
    key = (d.get('opencode-go') or {}).get('key') or (d.get(os.environ.get('AGENT_FALLBACK_PROVIDER','opencode-go')) or {}).get('key','')
    print(key or '')
except Exception: print('')
" 2>/dev/null || true)"
    cat > "$ENV_FILE" <<EOF
WECHAT_BRIDGE_SEND_PORT=$SEND_PORT
WECHAT_BRIDGE_OWNER=$OWNER_ID
WECHAT_BRIDGE_DAEMON_PY=$PROJECT_DIR/.venv/bin/python
WECHAT_BRIDGE_DAEMON=$PROJECT_DIR/chiguo_daemon.py
WECHAT_BRIDGE_AGENT_RUN=$PROJECT_DIR/scripts/agent-run.mjs
WECHAT_BRIDGE_STORAGE=$WX_STORAGE
OPENCODE_API_KEY=$AGENT_KEY
EOF
    chmod 600 "$ENV_FILE"
}

do_install() {
    say "安装 wechat-bridge（可移植：wechatbot SDK 克隆到 \$HOME/wechatbot，登录态随 chiguo 仓库）..."
    [ -x "$PROJECT_DIR/.venv/bin/python" ] || fail "chiguo .venv 不存在，请先跑 deploy.sh"
    mkdir -p "$WX_STORAGE" && chmod 700 "$AUTH_DIR" "$WX_STORAGE" 2>/dev/null || true
    if [ ! -d "$WECHATBOT_DIR/.git" ]; then
        say "克隆 wechatbot SDK → $WECHATBOT_DIR ..."
        git clone --depth 1 "$WECHATBOT_REPO" "$WECHATBOT_DIR" || fail "git clone wechatbot 失败（$WECHATBOT_REPO）"
    else
        say "wechatbot SDK 已存在，更新中..."
        git -C "$WECHATBOT_DIR" pull --ff-only >/dev/null 2>&1 || warn "wechatbot pull 失败（保持现有版本继续）"
    fi
    if [ ! -d "$WECHATBOT_DIR/nodejs" ]; then
        fail "wechatbot 仓库缺少 nodejs/ SDK 目录，无法安装"
    fi
    # SDK 是 TS 源码：dist 被上游 gitignore，npm install file: 只拷源码不构建 →
    # 缺 dist/index.js 时 bridge.mjs 顶层 import 必挂（干净部署实测）。与 install_agent.sh
    # 对 memory-lancedb-pro 的 clone+build 同款处理；幂等：dist 已存在跳过。
    if [ ! -f "$WECHATBOT_DIR/nodejs/dist/index.js" ]; then
        say "SDK 未构建（dist 缺失），执行 npm install && npm run build ..."
        ( cd "$WECHATBOT_DIR/nodejs" && npm install --no-fund --no-audit >/dev/null 2>&1 \
            && npm run build >/dev/null 2>&1 ) \
            || fail "SDK 构建失败（手工: cd $WECHATBOT_DIR/nodejs && npm install && npm run build）"
    else
        say "SDK 已构建（dist 存在，跳过 build）"
    fi
    say "安装 npm 依赖（@wechatbot/wechatbot <- $WECHATBOT_DIR/nodejs）..."
    ( cd "$BRIDGE_DIR" && npm install "@wechatbot/wechatbot@file:$WECHATBOT_DIR/nodejs" --no-fund --no-audit >/dev/null ) \
        || fail "npm install 失败"
    write_env
    say "install 完成（.env 已生成；登录态目录: $WX_STORAGE（集中认证，可随 ~/.chiguo/auth/ 迁移））"
    if has_credentials; then
        say "检测到已有登录态 → 启动后将自动复用（失效则自动打印二维码重登）"
    else
        warn "无登录态（首次部署）→ start 后按提示扫码登录；登录态仅本地保留（不进 git）"
    fi
}

do_start() {
    if pgrep -f "node .*bridge.mjs" >/dev/null 2>&1; then
        say "bridge 已在运行（PID $(pgrep -f 'node .*bridge.mjs' | head -1)）"
        return 0
    fi
    [ -f "$ENV_FILE" ] || { warn "缺少 .env（先运行: bash scripts/wechat-bridge.sh install）"; return 1; }
    [ -d "$BRIDGE_DIR/node_modules/@wechatbot" ] || { warn "缺少 node_modules（先运行: bash scripts/wechat-bridge.sh install）"; return 1; }
    mkdir -p "$(dirname "$LOG_FILE")"
    say "启动 bridge（日志: $LOG_FILE）..."
    ( cd "$BRIDGE_DIR" && setsid nohup node --env-file="$ENV_FILE" bridge.mjs >> "$LOG_FILE" 2>&1 < /dev/null & disown )
    for _ in 1 2 3 4 5; do
        sleep 1
        pgrep -f "node .*bridge.mjs" >/dev/null 2>&1 && break
    done
    if ! pgrep -f "node .*bridge.mjs" >/dev/null 2>&1; then
        warn "启动失败，请查看日志: tail -20 $LOG_FILE"
        return 1
    fi
    say "bridge 已启动（PID $(pgrep -f 'node .*bridge.mjs' | head -1)）"
    if has_credentials; then
        say "复用已有登录态；若过期会自动打印新二维码"
    else
        say "首次运行，请扫码登录（二维码见 $LOG_FILE）"
    fi
}

do_stop() {
    local pids
    pids="$(pgrep -f 'node .*bridge.mjs' || true)"
    if [ -z "$pids" ]; then
        say "bridge 未在运行"
        return 0
    fi
    kill $pids 2>/dev/null || true
    sleep 1
    pgrep -f 'node .*bridge.mjs' >/dev/null 2>&1 && { pkill -9 -f 'node .*bridge.mjs' 2>/dev/null || true; }
    say "bridge 已停止"
}

do_status() {
    local rc=0
    if pgrep -f "node .*bridge.mjs" >/dev/null 2>&1; then
        say "运行中（PID $(pgrep -f 'node .*bridge.mjs' | head -1)，端口 $SEND_PORT）"
    else
        warn "未运行（bash scripts/wechat-bridge.sh start）"
        rc=1
    fi
    if has_credentials; then
        local acct
        acct="$(sed -n 's/.*"userId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$WX_STORAGE/credentials.json" 2>/dev/null | head -1)"
        say "登录态存在${acct:+（账号 $acct）}（仅本地保留，不进 git）"
    else
        warn "无登录态（start 后扫码登录）"
        rc=1
    fi
    return $rc
}

do_login() {
    # 强制重新扫码：删除登录态后 start（旧会话服务端可能已失效）
    say "清除登录态并重启（打印新二维码）..."
    do_stop
    rm -f "$WX_STORAGE/"*.json
    do_start
}

case "${1:-}" in
    install) do_install ;;
    start) do_start ;;
    stop) do_stop ;;
    status) do_status ;;
    login) do_login ;;
    *)
        echo "用法: bash scripts/wechat-bridge.sh <install|start|stop|status|login>"
        exit 1 ;;
esac
