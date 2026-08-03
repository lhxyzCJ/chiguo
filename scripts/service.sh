#!/usr/bin/env bash
# ============================================================
# service.sh — 迟菓服务统一管理（ollama + wechat-bridge）
# 模式: autostart（systemd 开机自启）| temp（临时启动，不注册自启）
# 子命令: autostart|temp|status|stop|uninstall；均支持 --dry-run（只报告不改）
# 退出码: 0=OK  1=警告/待办  2=严重
# 测试注入: CHIGUO_REPO_OVERRIDE / CHIGUO_SYSTEMD_DIR / CHIGUO_SYSTEMCTL / CHIGUO_PID_DIR
# ============================================================
set -uo pipefail

REPO="${CHIGUO_REPO_OVERRIDE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRIDGE_DIR="$REPO/wechat-bridge"
ENV_FILE="$BRIDGE_DIR/.env"
SYSTEMD_DIR="${CHIGUO_SYSTEMD_DIR:-/etc/systemd/system}"
SYSTEMCTL="${CHIGUO_SYSTEMCTL:-systemctl}"
PID_DIR="${CHIGUO_PID_DIR:-$HOME/.chiguo/run}"
BRIDGE_UNIT="$SYSTEMD_DIR/chiguo-bridge.service"
TEMP_PIDFILE="$PID_DIR/bridge-temp.pid"
LOG_FILE="${WECHAT_BRIDGE_LOG:-/tmp/opencode/wechat-bridge-temp.log}"
DRY=0
CMD=""

say() { printf '\033[1;32m[chiguo-service]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chiguo-service]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[chiguo-service]\033[0m %s\n' "$*"; exit 2; }

usage() {
  cat <<'EOF'
用法: bash scripts/service.sh <autostart|temp|status|stop|uninstall> [--dry-run]
  autostart  注册 systemd 开机自启（ollama + wechat-bridge）
  temp       临时启动 bridge（不注册自启，nohup 后台，写 pidfile）
  status     展示 systemd / temp / ollama 三态
  stop       停止 systemd 服务与 temp 进程
  uninstall  停止并移除 systemd unit（登录态保留）
EOF
}

NODE="$(command -v node || true)"

write_unit() {
  local tmp="$BRIDGE_UNIT.tmp"
  cat > "$tmp" <<EOF
[Unit]
Description=Chiguo WeChat Bridge
After=network-online.target ollama.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$BRIDGE_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$NODE bridge.mjs
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF
  cat "$tmp"
  rm -f "$tmp"
}

do_autostart() {
  [ -n "$NODE" ] || fail "缺少 node（需先安装 Node.js）"
  [ -f "$ENV_FILE" ] || { warn "缺少 .env（先运行: bash scripts/wechat-bridge.sh install）"; exit 1; }
  say "systemd unit 模板（chiguo-bridge.service）:"
  write_unit
}

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    autostart|temp|status|stop|uninstall) CMD="$arg" ;;
    *) usage; exit 2 ;;
  esac
done
[ -n "$CMD" ] || { usage; exit 2; }

case "$CMD" in
  autostart) do_autostart ;;
  *) usage; exit 2 ;;
esac
