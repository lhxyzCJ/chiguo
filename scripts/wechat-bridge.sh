#!/usr/bin/env bash
# ============================================================
# wechat-bridge.sh — 迟菓微信桥（wechatbot iLink SDK）管理脚本
# 可移植：任意机器 pull chiguo 仓库后即可安装启动。
# 用法: bash scripts/wechat-bridge.sh <install|start|stop|status|login>
# 退出码: 0=OK  1=可恢复问题（如未登录）  2=严重问题
# 幂等：install/start 重复运行安全。
#
# 安全：write_env 生成的 wechat-bridge/.env 含明文 LLM API key
# （OPENCODE_API_KEY），仅 root 0600 权限；勿提交 git、勿进备份外传。
# 如需变更 key，改 ~/.pi/agent/auth.json 的 opencode-go 条目并重跑 install。
# ============================================================
set -uo pipefail

PROJECT_DIR="${CHIGUO_REPO_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRIDGE_DIR="$PROJECT_DIR/wechat-bridge"
ENV_FILE="$BRIDGE_DIR/.env"
# 精确匹配我们的 bridge.mjs 进程（绝对路径 + 转义 .）："node .*bridge.mjs" 正则过宽，会误匹配他项目同名脚本
BRIDGE_PGREP="node .*${BRIDGE_DIR}/bridge\.mjs"
LOG_FILE="${WECHAT_BRIDGE_LOG:-/tmp/opencode/wechat-bridge.log}"
# vendor SDK 随仓库入库（wechat-bridge/vendor/wechatbot），install 默认直接用；以下仅作“更新参考”时的上游 clone 参数
WECHATBOT_DIR="${WECHATBOT_DIR:-$HOME/wechatbot}"
WECHATBOT_REPO="${WECHATBOT_REPO:-https://github.com/lhxyzCJ/wechatbot.git}"
VENDOR_DIR="$BRIDGE_DIR/vendor/wechatbot"
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

# #review: 默认生成随机共享 token（同机任意进程也能打 /agent/prompt 消耗 LLM 配额）。
# 升级：#191 起未配置 token 直接 FATAL 拒绝启动（main() 校验），此处生成保证能启动。
# 幂等：已配置的 token 保留（重跑 install 不覆盖）。调用方：tick.sh 读 .env、daemon _loop_send 读 env。
BRIDGE_TOKEN="$(sed -n 's/^WECHAT_BRIDGE_TOKEN=//p' "$ENV_FILE" 2>/dev/null | head -1 || true)"
if [ -z "$BRIDGE_TOKEN" ]; then
  BRIDGE_TOKEN="$(openssl rand -hex 16 2>/dev/null || date +%s%N | md5sum | head -c 32)"
fi

write_env() {
    mkdir -p "$BRIDGE_DIR"
    # pi 生成需要 LLM key：~/.pi/agent/auth.json——优先 opencode-go 条目
    # 无则回退 [host].provider 条目（install_agent.sh 写入）
    AGENT_FALLBACK_PROVIDER="$(sed -n 's/^[[:space:]]*provider *= *"\([^"]*\)".*/\1/p' "$PROJECT_DIR/chiguo_proactive.toml" | head -1 || true)"
    [ -n "$AGENT_FALLBACK_PROVIDER" ] || AGENT_FALLBACK_PROVIDER=opencode-go
    # H-1: 用仓库 venv python（do_install 已校验 .venv 存在），避免裸 python3 在
    # 精简 PATH 下缺失 → key 静默为空；AGENT_KEY 空时从现有 .env 幂等回填，不覆盖旧有效值。
    AGENT_KEY="$(AGENT_FALLBACK_PROVIDER="$AGENT_FALLBACK_PROVIDER" "$PROJECT_DIR/.venv/bin/python" -c "
import json,os
try:
    d=json.load(open(os.path.expanduser('~/.pi/agent/auth.json')))
    key = (d.get('opencode-go') or {}).get('key') or (d.get(os.environ.get('AGENT_FALLBACK_PROVIDER','opencode-go')) or {}).get('key','')
    print(key or '')
except Exception: print('')
" 2>/dev/null || true)"
    if [ -z "$AGENT_KEY" ] && [ -f "$ENV_FILE" ]; then
      AGENT_KEY="$(sed -n 's/^OPENCODE_API_KEY=//p' "$ENV_FILE" 2>/dev/null | head -1 || true)"
    fi
    # umask 077：文件创建即为 600，避免 chmod 前窗口期 key 以默认权限落盘
    ( umask 077; cat > "$ENV_FILE" <<EOF
WECHAT_BRIDGE_SEND_PORT=$SEND_PORT
WECHAT_BRIDGE_OWNER=$OWNER_ID
WECHAT_BRIDGE_DAEMON_PY=$PROJECT_DIR/.venv/bin/python
WECHAT_BRIDGE_DAEMON=$PROJECT_DIR/chiguo_daemon.py
WECHAT_BRIDGE_AGENT_RUN=$PROJECT_DIR/scripts/agent-run.mjs
WECHAT_BRIDGE_AGENT_RPC=1
WECHAT_BRIDGE_TOKEN=$BRIDGE_TOKEN
WECHAT_BRIDGE_STORAGE=$WX_STORAGE
# LLM API key（与 ~/.pi/agent/auth.json 的 opencode-go 条目相同；.env 仅为 bridge 兼容冗余，勿外传/勿进 git）
OPENCODE_API_KEY=$AGENT_KEY
EOF
    )
    chmod 600 "$ENV_FILE"
}

# 更新参考：从上游 lhxyzCJ/wechatbot（实测链 lhxyzCJ → corespeed-io/wechatbot）克隆并把 nodejs/ + LICENSE 覆盖到 vendor。
# 默认 install 不 clone（vendor 已随仓库入库）；仅在显式要求时用于同步上游新版本。
do_update_vendor_from_upstream() {
    say "从上游更新 vendor SDK（参考用）：$WECHATBOT_REPO → $WECHATBOT_DIR → $VENDOR_DIR ..."
    mkdir -p "$WECHATBOT_DIR" "$VENDOR_DIR"
    git clone --depth 1 "$WECHATBOT_REPO" "$WECHATBOT_DIR" 2>/dev/null \
      || { [ -d "$WECHATBOT_DIR/.git" ] && ( cd "$WECHATBOT_DIR" && git pull --ff-only >/dev/null 2>&1 ) \
        || fail "更新失败（先手工清理 $WECHATBOT_DIR）"; }
    [ -d "$WECHATBOT_DIR/nodejs" ] || fail "上游仓库缺少 nodejs/ SDK 目录"
    # nodejs/ 整目录拷贝（含 src/tests/examples/config），并在 vendor 中放置 fork 根 LICENSE（MIT 合规）
    cp -r "$WECHATBOT_DIR/nodejs/." "$VENDOR_DIR/"
    cp -f "$WECHATBOT_DIR/LICENSE" "$VENDOR_DIR/LICENSE"
    # 清掉更新产生的构建产物；package-lock.json 已跟踪入库，更新后须刷新两处锁并一并提交
    # （刷新锁必须用 npm install——npm ci 只读锁不写锁；桥经 file: 依赖 vendor，vendor 变化会连带桥锁失效）
    rm -rf "$VENDOR_DIR/dist" "$VENDOR_DIR/node_modules"
    say "vendor SDK 已从上游刷新（src 已覆盖；LICENSE 随 vendor 保留）"
    say "后续手工刷新锁后提交：cd wechat-bridge/vendor/wechatbot && npm install --no-fund --no-audit && cd ../.. && npm install --no-fund --no-audit && git add wechat-bridge/package-lock.json wechat-bridge/vendor/wechatbot/package-lock.json"
}

do_install() {
    say "安装 wechat-bridge（vendor SDK 随仓库：wechat-bridge/vendor/wechatbot）..."
    [ -x "$PROJECT_DIR/.venv/bin/python" ] || fail "chiguo .venv 不存在，请先跑 deploy.sh"
    mkdir -p "$WX_STORAGE" && chmod 700 "$AUTH_DIR" "$WX_STORAGE" 2>/dev/null || true
    [ -f "$VENDOR_DIR/src/index.ts" ] || fail "vendor SDK 缺失（$VENDOR_DIR/src/index.ts）——不应删除仓库内 vendor 源码"
    # SDK 是 TS 源码：dist 被忽略不入库 → 缺 dist/index.js 时先 npm ci 确定性安装 + tsc 构建（与 install_agent.sh 幂等）
    if [ ! -f "$VENDOR_DIR/dist/index.js" ]; then
        say "构建 vendor SDK（dist 缺失，npm ci + npm run build）..."
        ( cd "$VENDOR_DIR" && npm ci --no-fund --no-audit >/dev/null 2>&1 \
            && npm run build >/dev/null 2>&1 ) \
            || fail "vendor SDK 构建失败（手工: cd $VENDOR_DIR && npm ci && npm run build）"
    else
        say "vendor SDK 已构建（dist 存在，跳过 build）"
    fi
    say "安装 npm 依赖（@wechatbot/wechatbot <- ./vendor/wechatbot）..."
    ( cd "$BRIDGE_DIR" && npm ci --no-fund --no-audit >/dev/null ) \
        || fail "npm ci 失败"
    write_env
    say "install 完成（.env 已生成；登录态目录: $WX_STORAGE（集中认证，可随 ~/.chiguo/auth/ 迁移））"
    if has_credentials; then
        say "检测到已有登录态 → 启动后将自动复用（失效则自动打印二维码重登）"
    else
        warn "无登录态（首次部署）→ start 后按提示扫码登录；登录态仅本地保留（不进 git）"
    fi
}

do_start() {
    if pgrep -f "$BRIDGE_PGREP" >/dev/null 2>&1; then
        say "bridge 已在运行（PID $(pgrep -f "$BRIDGE_PGREP" | head -1)）"
        return 0
    fi
    [ -f "$ENV_FILE" ] || { warn "缺少 .env（先运行: bash scripts/wechat-bridge.sh install）"; return 1; }
    [ -d "$BRIDGE_DIR/node_modules/@wechatbot" ] || { warn "缺少 node_modules（先运行: bash scripts/wechat-bridge.sh install）"; return 1; }
    mkdir -p "$(dirname "$LOG_FILE")"
    say "启动 bridge（日志: $LOG_FILE）..."
    # 以绝对路径启动 → 进程 cmdline 含 "$BRIDGE_DIR/bridge.mjs"，供 BRIDGE_PGREP 精确匹配
    ( cd "$BRIDGE_DIR" && setsid nohup node --env-file="$ENV_FILE" "$BRIDGE_DIR/bridge.mjs" >> "$LOG_FILE" 2>&1 < /dev/null & disown )
    for _ in 1 2 3 4 5; do
        sleep 1
        pgrep -f "$BRIDGE_PGREP" >/dev/null 2>&1 && break
    done
    if ! pgrep -f "$BRIDGE_PGREP" >/dev/null 2>&1; then
        warn "启动失败，请查看日志: tail -20 $LOG_FILE"
        return 1
    fi
    say "bridge 已启动（PID $(pgrep -f "$BRIDGE_PGREP" | head -1)）"
    if has_credentials; then
        say "复用已有登录态；若过期会自动打印新二维码"
    else
        say "首次运行，请扫码登录（二维码见 $LOG_FILE）"
    fi
}

do_stop() {
    local pids
    pids="$(pgrep -f "$BRIDGE_PGREP" || true)"
    if [ -z "$pids" ]; then
        say "bridge 未在运行"
        return 0
    fi
    kill $pids 2>/dev/null || true
    sleep 1
    pgrep -f "$BRIDGE_PGREP" >/dev/null 2>&1 && { pkill -9 -f "$BRIDGE_PGREP" 2>/dev/null || true; }
    say "bridge 已停止"
}

do_status() {
    local rc=0
    if pgrep -f "$BRIDGE_PGREP" >/dev/null 2>&1; then
        say "运行中（PID $(pgrep -f "$BRIDGE_PGREP" | head -1)，端口 $SEND_PORT）"
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
    # #224 context_token 新鲜度检查：主动发送前置条件。context_token 由微信服务端签发，
    # 无公开 TTL，实测最后一次收到用户消息后约 35h 失效；每次收到用户消息自动刷新
    # （SDK remember() → context_tokens.json mtime = 最近刷新时刻）。过期症状：
    # 主动发送报 [send error] prepare failed —— 从微信给机器人发一条消息即恢复，无需重扫码。
    local ctx_file="$WX_STORAGE/context_tokens.json"
    if [ -f "$ctx_file" ]; then
        local age_h
        age_h=$(( ($(date +%s) - $(stat -c %Y "$ctx_file")) / 3600 ))
        if [ "$age_h" -ge 24 ]; then
            warn "context_token 已 ${age_h}h 未刷新（微信侧无公开 TTL，实测约 35h 过期）→ 主动发送将报 prepare failed；从微信给机器人发一条消息即刷新恢复，无需重扫码"
        else
            say "context_token 新鲜（${age_h}h 前刷新；收到用户消息自动续期）"
        fi
    else
        warn "无 context_tokens.json → 尚未收到过用户消息，主动发送会因缺 context_token 失败；先发一条微信给机器人即可"
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
    install)
        # 默认 vendor 优先（不 clone）。`install update` 或 WECHATBOT_BOOTSTRAP_FROM_CLONE=1 →
        # 先从上游克隆覆盖 vendor（仅作更新参考，非必需）。
        if [ "${2:-}" = "update" ] || [ "${WECHATBOT_BOOTSTRAP_FROM_CLONE:-0}" = "1" ]; then
            do_update_vendor_from_upstream
        fi
        do_install ;;
    start) do_start ;;
    stop) do_stop ;;
    status) do_status ;;
    login) do_login ;;
    *)
        echo "用法: bash scripts/wechat-bridge.sh <install [update]|start|stop|status|login>"
        exit 1 ;;
esac
