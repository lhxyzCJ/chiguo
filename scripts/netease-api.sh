#!/usr/bin/env bash
# ============================================================
# netease-api.sh — 网易云 API 服务（api-enhanced）管理脚本
# 上游: NeteaseCloudMusicApiEnhanced/api-enhanced（Binaryify/
#       NeteaseCloudMusicApi 2024-04 归档后的社区继承版, MIT）
# 用法: bash scripts/netease-api.sh <install|start|stop|status>
# 退出码: 0=OK  1=可恢复问题  2=严重问题
# 幂等: install 重复运行安全（package.json version 匹配即跳过）
# 测试注入: NETEASE_API_DIR / NETEASE_API_UNIT / NETEASE_API_BASE
# ============================================================
set -uo pipefail

NETEASE_API_DIR="${NETEASE_API_DIR:-/opt/netease-api}"
NETEASE_API_REPO="${NETEASE_API_REPO:-https://github.com/NeteaseCloudMusicApiEnhanced/api-enhanced.git}"
# 默认跟随上游最新 tag（install 时动态解析）；显式赋值 NETEASE_API_TAG=vX.Y.Z 可锁版本（测试注入/回滚）
NETEASE_API_TAG="${NETEASE_API_TAG:-}"
NETEASE_API_BASE="${NETEASE_API_BASE:-http://localhost:3000}"
NETEASE_API_UNIT="${NETEASE_API_UNIT:-/etc/systemd/system/netease-api.service}"

say() { printf '\033[1;32m[netease-api]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[netease-api]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[netease-api]\033[0m %s\n' "$*"; exit 2; }

installed_version() {
  node -p "require('$NETEASE_API_DIR/package.json').version" 2>/dev/null || echo ""
}

# 解析上游最新 release tag（v\d+.\d+.\d+，sort -V 取最大）
resolve_latest_tag() {
  # timeout 兜底：网络黑洞/防火墙丢包时 git ls-remote 默认可挂起数分钟（同 curl -m 语义）
  timeout 15 git ls-remote --tags --refs "$NETEASE_API_REPO" 2>/dev/null \
    | grep -oE 'refs/tags/v[0-9]+\.[0-9]+\.[0-9]+$' | sed 's#refs/tags/##' \
    | sort -V | tail -1
}

check_health() {
  curl -s -m 5 "$NETEASE_API_BASE/login/status" 2>/dev/null | grep -qE '"code":200([^0-9]|$)'
}

wait_healthy() {
  local tries=1
  while [ $tries -le 5 ]; do
    check_health && return 0
    sleep 2
    tries=$((tries + 1))
  done
  return 1
}

write_unit() {
  local tag="${1:-unknown}"
  local tmp="$NETEASE_API_UNIT.tmp"
  cat > "$tmp" <<EOF
[Unit]
Description=Netease Cloud Music API (api-enhanced $tag)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$NETEASE_API_DIR
ExecStart=/usr/bin/env node app.js
Environment=PORT=3000
# issue #85: 仅回环监听，不对局域网暴露
Environment=HOST=127.0.0.1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  if [ -f "$NETEASE_API_UNIT" ] && cmp -s "$tmp" "$NETEASE_API_UNIT"; then
    rm -f "$tmp"
    say "systemd unit 已存在且一致"
    return 0
  fi
  mv "$tmp" "$NETEASE_API_UNIT"
  say "systemd unit 已写入: $NETEASE_API_UNIT"
}

do_install() {
  if [ "$(id -u)" != "0" ]; then
    warn "需 root 权限（写 $NETEASE_API_DIR 与 $NETEASE_API_UNIT）"
    return 1
  fi
  command -v node >/dev/null 2>&1 || { warn "缺少 node（api-enhanced 需要 Node.js）"; return 1; }
  command -v npm >/dev/null 2>&1 || { warn "缺少 npm"; return 1; }

  local tag ver
  # 默认跟随上游最新 tag；NETEASE_API_TAG 显式赋值时锁版本（测试注入/回滚）
  tag="${NETEASE_API_TAG:-}"
  if [ -z "$tag" ]; then
    if ! tag="$(resolve_latest_tag)"; then
      if [ -n "$(installed_version)" ]; then
        # 离线/解析失败但已有有效安装 → 保持现有，不破坏已运行服务
        warn "无法解析上游最新 tag（git ls-remote 失败），保持现有安装"
        return 0
      fi
      warn "无法解析上游最新 tag（git ls-remote 失败）"
      return 1
    fi
    say "跟随上游最新: $tag"
  else
    say "锁定版本: $tag"
  fi
  ver="$(installed_version)"
  if [ -n "$ver" ] && { [ "$ver" = "$tag" ] || [ "v$ver" = "$tag" ]; }; then
    say "已安装 $tag（$NETEASE_API_DIR），跳过安装"
  else
    if [ ! -d "$NETEASE_API_DIR/.git" ]; then
      say "克隆 api-enhanced → $NETEASE_API_DIR ..."
      git clone --depth 1 --branch "$tag" "$NETEASE_API_REPO" "$NETEASE_API_DIR" \
        || fail "git clone 失败（$NETEASE_API_REPO）"
    else
      say "仓库已存在，拉取 tag $tag ..."
      git -C "$NETEASE_API_DIR" fetch origin tag "$tag" --depth 1 \
        || warn "fetch 失败（保持现有版本继续）"
      if ! git -C "$NETEASE_API_DIR" checkout "$tag" 2>/dev/null; then
        warn "checkout $tag 失败（保持现有版本继续），回落实际运行版本"
        tag="v$(installed_version)"
      fi
    fi
    # 代码版本切换（首次安装或升级）依赖可能变化 → 总是 npm install 刷新，避免新代码跑旧依赖
    say "安装/刷新 npm 依赖（代码版本切换时依赖可能变化）..."
    ( cd "$NETEASE_API_DIR" && npm install --no-fund --no-audit >/dev/null ) \
      || fail "npm install 失败"
  fi

  write_unit "$tag"
  systemctl daemon-reload || warn "daemon-reload 失败"
  systemctl enable --now netease-api || warn "enable/start 失败"

  local ok=0
  if wait_healthy; then ok=1; fi
  if [ "$ok" = 1 ]; then
    say "API 服务健康 ✓（$NETEASE_API_BASE 响应正常）"
  else
    warn "服务已启动但健康检查未通过（curl $NETEASE_API_BASE/login/status 排查）"
    return 1
  fi
}

do_start() {
  if [ ! -f "$NETEASE_API_UNIT" ]; then
    warn "systemd unit 不存在（先运行: bash scripts/netease-api.sh install）"
    return 1
  fi
  systemctl start netease-api
  if wait_healthy; then say "服务已启动 ✓"; else warn "启动但健康检查未通过"; return 1; fi
}

do_stop() {
  systemctl stop netease-api 2>/dev/null || true
  systemctl disable netease-api 2>/dev/null || true
  say "服务已停止并取消开机自启"
}

do_status() {
  local rc=0
  local active ver upstream
  active="$(systemctl is-active netease-api 2>/dev/null || true)"
  ver="$(installed_version)"
  if [ -z "${NETEASE_API_TAG:-}" ]; then
    upstream="$(resolve_latest_tag 2>/dev/null || true)"
    if [ -n "$upstream" ]; then
      if [ -n "$ver" ] && { [ "$ver" = "$upstream" ] || [ "v$ver" = "$upstream" ]; }; then
        say "上游最新: $upstream（当前已是最新 $ver）"
      else
        say "上游最新: $upstream（当前 $ver；有新版: bash scripts/netease-api.sh install）"
      fi
    fi
  fi
  if [ "$active" = "active" ]; then
    say "运行中 ✓（$NETEASE_API_DIR，version $ver）"
  else
    warn "未运行（systemctl is-active = ${active:-unknown}；bash scripts/netease-api.sh install）"
    rc=1
  fi
  if check_health; then
    say "API 健康 ✓（$NETEASE_API_BASE）"
  else
    warn "API 不可达（$NETEASE_API_BASE）"
    rc=1
  fi
  return $rc
}

case "${1:-}" in
  install) do_install ;;
  start) do_start ;;
  stop) do_stop ;;
  status) do_status ;;
  *)
    echo "用法: bash scripts/netease-api.sh <install|start|stop|status>"
    exit 1 ;;
esac
