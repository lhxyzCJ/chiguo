#!/usr/bin/env bash
# service.sh 桩测试：fake systemctl/node/curl + 临时 REPO/SYSTEMD_DIR/PID_DIR，
# 验证 dry-run 只读、unit 模板、幂等、互斥接管、status 三态、uninstall、错误路径（用例见各 Task）
set -uo pipefail
TMP="$(mktemp -d /tmp/chiguo-service-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

mkdir -p "$TMP/bin" "$TMP/repo/wechat-bridge" "$TMP/repo/scripts" "$TMP/systemd" "$TMP/pid" "$TMP/home"
touch "$TMP/repo/wechat-bridge/.env"   # .env 由 wechat-bridge.sh install 生成；错误路径用例会移除
export CHIGUO_REPO_OVERRIDE="$TMP/repo"
export CHIGUO_SYSTEMD_DIR="$TMP/systemd"
export CHIGUO_PID_DIR="$TMP/pid"
export HOME="$TMP/home"
CALLS_LOG="$TMP/calls.log"
export CALLS_LOG
: > "$CALLS_LOG"
PATH="$TMP/bin:$PATH"

# ── 假 node：记录调用；有 node 版本（错误路径用例通过 PATH 移除）──
cat > "$TMP/bin/node" <<'STUB'
#!/usr/bin/env bash
echo "node $*" >> "$CALLS_LOG"
exit 0
STUB
chmod +x "$TMP/bin/node"

# ── 假 curl：/api/tags 输出 OK（ollama 健康判定）──
cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "$CALLS_LOG"
if printf '%s' "$*" | grep -q "/api/tags"; then
  echo '{"models":[{"name":"qwen3-embedding"}]}'
  exit 0
fi
exit 1
STUB
chmod +x "$TMP/bin/curl"

# ── 假 systemctl：is-active/is-enabled/stop/enable/daemon-reload 记录调用 ──
cat > "$TMP/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS_LOG"
case "$1" in
  is-active) [ -f "$ACTIVE_MARK" ] && exit 0 || exit 3 ;;
  is-enabled) exit 0 ;;
  daemon-reload) exit 0 ;;
  stop) exit 0 ;;
  enable) exit 0 ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$TMP/bin/systemctl"
export CHIGUO_SYSTEMCTL="$TMP/bin/systemctl"
SERVICE="$TMP/repo/scripts/service.sh"
cp "$(dirname "${BASH_SOURCE[0]}")/../scripts/service.sh" "$SERVICE"
chmod +x "$SERVICE"

# ── 用例 1: autostart --dry-run 只读，输出 unit 模板且不写文件 ──
OUT="$("$SERVICE" autostart --dry-run 2>&1)"
echo "$OUT" | grep -q "chiguo-bridge.service" || fail "dry-run 未展示 unit 名"
echo "$OUT" | grep -q "EnvironmentFile=$TMP/repo/wechat-bridge/.env" || fail "unit 模板缺 EnvironmentFile"
echo "$OUT" | grep -q "ExecStart=.*node bridge.mjs" || fail "unit 模板缺 ExecStart"
echo "$OUT" | grep -q "Restart=on-failure" || fail "unit 模板缺 Restart"
echo "$OUT" | grep -q "After=network-online.target ollama.service" || fail "unit 模板缺 After"
[ -z "$(ls -A "$TMP/systemd" 2>/dev/null)" ] || fail "dry-run 不应写 unit 文件"
grep -q "systemctl" "$CALLS_LOG" && fail "dry-run 不应调用 systemctl"
: > "$CALLS_LOG"
pass "dry-run 只读 + unit 模板断言"
