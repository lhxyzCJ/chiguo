#!/usr/bin/env bash
# ============================================================
# service.sh — 迟菓服务统一管理（ollama + wechat-bridge）
# 模式: autostart（systemd 开机自启）| temp（临时启动，不注册自启）
# 子命令: autostart|temp|status|stop|uninstall；均支持 --dry-run（只报告不改）
# 退出码: 0=OK  1=警告/待办  2=严重
# 测试注入: CHIGUO_REPO_OVERRIDE / CHIGUO_SYSTEMD_DIR / CHIGUO_SYSTEMCTL / CHIGUO_PID_DIR / CHIGUO_NODE
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

NODE="${CHIGUO_NODE-$(command -v node || true)}"

ollama_health() {
  curl -s -m 3 --noproxy '*' http://127.0.0.1:11434/api/tags 2>/dev/null | grep -q '"models"'
}

systemd_active() {
  "$SYSTEMCTL" is-active --quiet chiguo-bridge 2>/dev/null
}

temp_running() {
  [ -f "$TEMP_PIDFILE" ] || return 1
  local pid
  pid="$(cat "$TEMP_PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

kill_temp() {
  temp_running || { rm -f "$TEMP_PIDFILE"; return 0; }
  kill "$(cat "$TEMP_PIDFILE")" 2>/dev/null || true
  sleep 1
  temp_running && kill -9 "$(cat "$TEMP_PIDFILE")" 2>/dev/null || true
  rm -f "$TEMP_PIDFILE"
}

stop_systemd() {
  systemd_active || return 0
  "$SYSTEMCTL" stop chiguo-bridge 2>/dev/null
}

write_unit() {
  # 返回码: 0=unit 已改写（mv 成功，需 restart 加载新配置） 1=已存在且一致（未改写） 2=dry-run（未写）
  local tmp="$BRIDGE_UNIT.tmp"
  # 逐行 printf（不用未加引号的 heredoc）：变量值只作 %s 参数原样插入，
  # 避免 $BRIDGE_DIR/$ENV_FILE 含特殊字符时被 shell 二次展开或 printf 格式串注入
  printf '%s\n' \
    '[Unit]' \
    'Description=Chiguo WeChat Bridge' \
    'After=network-online.target ollama.service' \
    'Wants=network-online.target' \
    '' \
    '[Service]' \
    'Type=simple' \
    "WorkingDirectory=$BRIDGE_DIR" \
    "EnvironmentFile=$ENV_FILE" \
    "ExecStart=$NODE $BRIDGE_DIR/bridge.mjs" \
    'Restart=on-failure' \
    '' \
    '[Install]' \
    'WantedBy=multi-user.target' \
    > "$tmp"
  if [ -f "$BRIDGE_UNIT" ] && cmp -s "$tmp" "$BRIDGE_UNIT"; then
    rm -f "$tmp"
    say "systemd unit 已存在且一致"
    return 1
  fi
  if [ "$DRY" = 1 ]; then
    cat "$tmp"
    rm -f "$tmp"
    say "dry-run：unit 将写入 $BRIDGE_UNIT"
    return 2
  fi
  mv "$tmp" "$BRIDGE_UNIT"
  say "systemd unit 已写入: $BRIDGE_UNIT"
  return 0
}

require_root() {
  [ "$(id -u)" = 0 ] || fail "需 root 权限（写 $SYSTEMD_DIR；非 root 请用 sudo 或 temp 模式）"
}

do_autostart() {
  require_root
  [ -n "$NODE" ] || fail "缺少 node（需先安装 Node.js）"
  [ -f "$ENV_FILE" ] || { warn "缺少 .env（先运行: bash scripts/wechat-bridge.sh install）"; exit 1; }
  if [ "$DRY" = 1 ]; then
    say "dry-run 计划:"
    say "  1) systemctl enable --now ollama"
    say "  2) 清理残留 temp 进程（pidfile: $TEMP_PIDFILE）→ 释放 18790 端口"
    write_unit
    say "  3) systemctl daemon-reload + enable --now chiguo-bridge"
    return 0
  fi
  say "阶段 1: ollama 自启..."
  "$SYSTEMCTL" enable --now ollama 2>/dev/null \
    || warn "ollama enable 失败（无 ollama unit？可手动: systemctl enable --now ollama）"
  if ollama_health; then
    say "ollama 健康 ✓（11434 响应正常）"
  else
    warn "ollama 健康检查未通过（curl http://127.0.0.1:11434/api/tags 排查）"
  fi
  say "阶段 2: 互斥接管：清理 temp 残留（释放 18790 端口）..."
  if temp_running; then
    kill_temp
    say "已停止 temp 实例（互斥接管）"
  else
    say "无 temp 残留"
  fi
  say "阶段 3: wechat-bridge unit..."
  write_unit
  UNIT_CHANGED=$?
  "$SYSTEMCTL" daemon-reload
  "$SYSTEMCTL" enable --now chiguo-bridge 2>/dev/null \
    || { warn "chiguo-bridge enable/start 失败（bash scripts/wechat-bridge.sh status 排查）"; exit 1; }
  if [ "$UNIT_CHANGED" = 0 ]; then
    "$SYSTEMCTL" restart chiguo-bridge || warn "chiguo-bridge restart 失败"
    say "unit 已变更，重启 bridge 加载新配置"
  fi
  say "autostart 完成 ✓（开机自启: ollama + chiguo-bridge）"
}

do_temp() {
  [ -n "$NODE" ] || fail "缺少 node（需先安装 Node.js）"
  [ -f "$ENV_FILE" ] || { warn "缺少 .env（先运行: bash scripts/wechat-bridge.sh install）"; exit 1; }
  if [ "$DRY" = 1 ]; then
    say "dry-run 计划:"
    say "  1) systemctl stop chiguo-bridge（互斥接管，若在运行）"
    say "  2) nohup node --env-file=$ENV_FILE bridge.mjs（后台）"
    say "  3) pidfile: $TEMP_PIDFILE；日志: $LOG_FILE"
    return 0
  fi
  if temp_running; then
    say "temp 实例已在运行（pidfile: $TEMP_PIDFILE）"
    return 0
  fi
  say "互斥接管：停 systemd 实例（若在运行）..."
  stop_systemd
  if ollama_health; then
    say "ollama 健康 ✓"
  else
    warn "ollama 不在线（embedding 记忆库不可用；本模式不拉起，由 systemd 管理）"
  fi
  say "启动 temp bridge（日志: $LOG_FILE）..."
  mkdir -p "$PID_DIR" "$(dirname "$LOG_FILE")"
  (
    cd "$BRIDGE_DIR" || exit 1
    setsid nohup "$NODE" --env-file="$ENV_FILE" bridge.mjs >> "$LOG_FILE" 2>&1 < /dev/null &
    echo "$!" > "$TEMP_PIDFILE"
  )
  sleep 0.5
  if ! kill -0 "$(cat "$TEMP_PIDFILE" 2>/dev/null || echo 0)" 2>/dev/null; then
    rm -f "$TEMP_PIDFILE"
    warn "temp 启动后立即退出（日志: $LOG_FILE 排查）"
    return 1
  fi
  say "temp 已启动（PID $(cat "$TEMP_PIDFILE")；不注册开机自启；bash scripts/service.sh status 查看）"
}

do_status() {
  local rc=0
  if systemd_active; then
    say "systemd: active（chiguo-bridge.service）"
  else
    warn "systemd: inactive/未注册"
    rc=1
  fi
  if temp_running; then
    say "temp: running（pidfile: $TEMP_PIDFILE）"
  else
    warn "temp: 未运行"
  fi
  if ollama_health; then
    say "ollama: healthy（11434）"
  else
    warn "ollama: 不可用"
    rc=1
  fi
  return $rc
}

do_stop() {
  if [ "$DRY" = 1 ]; then
    say "dry-run 计划: systemctl stop chiguo-bridge + 清理 temp（$TEMP_PIDFILE）"
    return 0
  fi
  if systemd_active; then
    if stop_systemd; then
      say "systemd 实例已停止"
    else
      warn "systemd 停止失败（bash scripts/service.sh status 排查）"
    fi
  else
    say "systemd 实例未在运行"
  fi
  if temp_running; then
    kill_temp
    say "temp 实例已停止"
  else
    say "temp 实例未在运行"
  fi
}

do_uninstall() {
  require_root
  if [ "$DRY" = 1 ]; then
    say "dry-run 计划: stop + 删除 $BRIDGE_UNIT + daemon-reload（登录态 $HOME/.chiguo/auth/wechat/ 保留；不撤销 ollama enable）"
    return 0
  fi
  stop_systemd || true
  kill_temp || true
  if [ ! -f "$BRIDGE_UNIT" ]; then
    say "unit 不存在，无需删除"
    "$SYSTEMCTL" daemon-reload 2>/dev/null || true
    return 0
  fi
  rm -f "$BRIDGE_UNIT"
  "$SYSTEMCTL" daemon-reload
  say "uninstall 完成（unit 已删除；登录态保留；ollama 自启未动）"
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
  temp) do_temp ;;
  status) do_status ;;
  stop) do_stop ;;
  uninstall) do_uninstall ;;
  *) usage; exit 2 ;;
esac
