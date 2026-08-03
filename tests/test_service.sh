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

# ── 用例 2: autostart 写 unit + 调用链（enable ollama → daemon-reload → enable chiguo-bridge）──
"$SERVICE" autostart >/dev/null 2>&1 || fail "autostart 退出非 0"
[ -f "$TMP/systemd/chiguo-bridge.service" ] || fail "unit 未写入"
grep -q "enable --now ollama" "$CALLS_LOG" || fail "未 enable ollama"
grep -q "daemon-reload" "$CALLS_LOG" || fail "未 daemon-reload"
grep -q "enable --now chiguo-bridge" "$CALLS_LOG" || fail "未 enable chiguo-bridge"
: > "$CALLS_LOG"
pass "autostart 写 unit + systemctl 调用链"

# ── 用例 3: 幂等（重复 autostart 不重写 unit，两次调用间文件 mtime 不变）──
UNIT_MTIME="$(stat -c %Y "$TMP/systemd/chiguo-bridge.service")"
sleep 1
"$SERVICE" autostart >/dev/null 2>&1 || fail "重复 autostart 退出非 0"
[ "$(stat -c %Y "$TMP/systemd/chiguo-bridge.service")" = "$UNIT_MTIME" ] || fail "unit 被重复重写"
pass "autostart 幂等"

# ── 用例 4: temp 残留被杀（pidfile 指向存活进程时 autostart 清理）──
sleep 300 & FAKE_TEMP_PID=$!
echo "$FAKE_TEMP_PID" > "$TMP/pid/bridge-temp.pid"
"$SERVICE" autostart >/dev/null 2>&1 || fail "autostart 退出非 0"
[ ! -f "$TMP/pid/bridge-temp.pid" ] || fail "残留 temp pidfile 未被清理"
kill "$FAKE_TEMP_PID" 2>/dev/null || true
pass "autostart 清理 temp 残留"

# ── 用例 5: temp 先停 systemd 实例（互斥接管），再启动并写 pidfile ──
touch "$TMP/active"   # fake systemctl is-active → 0
export ACTIVE_MARK="$TMP/active"
: > "$CALLS_LOG"
"$SERVICE" temp >/dev/null 2>&1 || fail "temp 退出非 0"
grep -q "stop chiguo-bridge" "$CALLS_LOG" || fail "temp 未先停 systemd 实例"
grep -q "node --env-file=$TMP/repo/wechat-bridge/.env bridge.mjs" "$CALLS_LOG" || fail "temp 未启动 bridge"
[ -f "$TMP/pid/bridge-temp.pid" ] || fail "temp pidfile 未写入"
unset ACTIVE_MARK
rm -f "$TMP/pid/bridge-temp.pid"
pass "temp 互斥接管 + pidfile"

# ── 用例 6: temp 重复运行（pidfile 存在）→ 提示并退出 0，不重复启动 ──
sleep 300 & FAKE_TEMP_PID=$!
echo "$FAKE_TEMP_PID" > "$TMP/pid/bridge-temp.pid"
: > "$CALLS_LOG"
OUT="$("$SERVICE" temp 2>&1)"
[ $? = 0 ] || fail "重复 temp 应退出 0"
echo "$OUT" | grep -q "已在运行" || fail "重复 temp 未提示已在运行"
grep -q "node --env-file" "$CALLS_LOG" && fail "重复 temp 不应再次启动"
kill "$FAKE_TEMP_PID" 2>/dev/null || true
rm -f "$TMP/pid/bridge-temp.pid"
pass "temp 幂等"

# ── 用例 7: temp 时 ollama 不在线 → warn 但继续启动 ──
cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "$CALLS_LOG"
exit 1
STUB
chmod +x "$TMP/bin/curl"
OUT="$("$SERVICE" temp 2>&1)"
echo "$OUT" | grep -q "ollama" || fail "ollama 不在线未 warn"
[ -f "$TMP/pid/bridge-temp.pid" ] || fail "ollama 不在线不应阻止 temp 启动"
rm -f "$TMP/pid/bridge-temp.pid"
pass "temp 对 ollama 降级 warn"
