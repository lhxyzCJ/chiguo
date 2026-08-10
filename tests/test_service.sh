#!/usr/bin/env bash
# service.sh 桩测试：fake systemctl/node/curl + 临时 REPO/SYSTEMD_DIR/PID_DIR，
# 验证 dry-run 只读、unit 模板、幂等、互斥接管、status 三态、uninstall、错误路径（用例见各 Task）
set -uo pipefail
TMP="$(mktemp -d /tmp/chiguo-service-test.XXXXXX)"
trap 'kill $(cat "$TMP/pid/bridge-temp.pid" 2>/dev/null) 2>/dev/null || true; rm -rf "$TMP"' EXIT

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
PATH="$TMP/bin:$PATH"

# ── 假 id：root 视角（CI runner 非 root；用例 13 用 $TMP/nonroot 的 fake id 模拟非 root）──
cat > "$TMP/bin/id" <<'STUB'
#!/usr/bin/env bash
echo 0
STUB
chmod +x "$TMP/bin/id"

# ── 假 node：记录 "$0 $*"；模拟常驻（exec sleep）供 temp 存活校验 ──
cat > "$TMP/bin/node" <<'STUB'
#!/usr/bin/env bash
echo "$0 $*" >> "$CALLS_LOG"
exec sleep 300
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
  enable)
    # R15: 若 ENFORCE_TEMP_DEAD=1 且 temp pidfile 仍存在 → enable 必败
    #（证明 autostart 先 kill_temp 才启动 systemd 实例,防 18790 端口死锁）
    if [ -n "${ENFORCE_TEMP_DEAD:-}" ] && [ -f "${ENFORCE_TEMP_DEAD_PIDFILE:-}" ]; then
      echo "[fake-systemctl] enable 失败: temp 仍在运行" >> "$CALLS_LOG"
      exit 1
    fi
    exit 0 ;;
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

# ── 用例 4b: kill_temp 前置（R15）——enable chiguo-bridge 前 temp 必须已死（18790 端口死锁修复）──
sleep 300 & FAKE_TEMP_PID2=$!
echo "$FAKE_TEMP_PID2" > "$TMP/pid/bridge-temp.pid"
export ENFORCE_TEMP_DEAD=1 ENFORCE_TEMP_DEAD_PIDFILE="$TMP/pid/bridge-temp.pid"
if ! "$SERVICE" autostart >/dev/null 2>&1; then
  unset ENFORCE_TEMP_DEAD ENFORCE_TEMP_DEAD_PIDFILE
  kill "$FAKE_TEMP_PID2" 2>/dev/null || true
  fail "temp 存活时 autostart 应先 kill_temp 再 enable（R15 顺序回归）"
fi
unset ENFORCE_TEMP_DEAD ENFORCE_TEMP_DEAD_PIDFILE
[ ! -f "$TMP/pid/bridge-temp.pid" ] || fail "残留 temp pidfile 未被清理"
kill "$FAKE_TEMP_PID2" 2>/dev/null || true
pass "autostart kill_temp 前置（R15: 释放 18790 端口）"

# ── 用例 5: temp 先停 systemd 实例（互斥接管），再启动并写 pidfile ──
touch "$TMP/active"   # fake systemctl is-active → 0
export ACTIVE_MARK="$TMP/active"
: > "$CALLS_LOG"
"$SERVICE" temp >/dev/null 2>&1 || fail "temp 退出非 0"
grep -q "stop chiguo-bridge" "$CALLS_LOG" || fail "temp 未先停 systemd 实例"
grep -q "node --env-file=$TMP/repo/wechat-bridge/.env bridge.mjs" "$CALLS_LOG" || fail "temp 未启动 bridge"
[ -f "$TMP/pid/bridge-temp.pid" ] || fail "temp pidfile 未写入"
kill "$(cat "$TMP/pid/bridge-temp.pid")" 2>/dev/null || true
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
kill "$(cat "$TMP/pid/bridge-temp.pid")" 2>/dev/null || true
rm -f "$TMP/pid/bridge-temp.pid"
pass "temp 对 ollama 降级 warn"

# ── 用例 7b: temp 使用 CHIGUO_NODE 注入路径启动 ──
cat > "$TMP/bin/fakenode" <<'STUB'
#!/usr/bin/env bash
echo "fake-node $*" >> "$CALLS_LOG"
exec sleep 300
STUB
chmod +x "$TMP/bin/fakenode"
: > "$CALLS_LOG"
CHIGUO_NODE="$TMP/bin/fakenode" "$SERVICE" temp >/dev/null 2>&1 || fail "temp 退出非 0"
grep -q "fake-node --env-file=$TMP/repo/wechat-bridge/.env bridge.mjs" "$CALLS_LOG" || fail "temp 未用 CHIGUO_NODE 注入路径"
kill "$(cat "$TMP/pid/bridge-temp.pid")" 2>/dev/null || true
rm -f "$TMP/pid/bridge-temp.pid"
pass "temp 使用 CHIGUO_NODE 注入路径"

# ── 用例 7c: temp 启动即死 → warn + 退出 1 + pidfile 清理 ──
mkdir -p "$TMP/bin-dead"
cat > "$TMP/bin-dead/node" <<'STUB'
#!/usr/bin/env bash
echo "$0 $*" >> "$CALLS_LOG"
exit 1
STUB
chmod +x "$TMP/bin-dead/node"
: > "$CALLS_LOG"
OUT="$(PATH="$TMP/bin-dead:$PATH" "$SERVICE" temp 2>&1)"
[ $? = 1 ] || fail "temp 启动即死应退出 1（实际 $?）"
echo "$OUT" | grep -q "立即退出" || fail "temp 启动即死未 warn"
[ ! -f "$TMP/pid/bridge-temp.pid" ] || fail "temp 启动即死 pidfile 未清理"
pass "temp 启动即死 → warn + 退出 1"

# ── 用例 8: status 三态（systemd active + temp 无 + ollama 健康）──
# 恢复 curl 假件（用例 7 已替换为全失败版）
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
sleep 300 & FAKE_TEMP_PID=$!
echo "$FAKE_TEMP_PID" > "$TMP/pid/bridge-temp.pid"
touch "$TMP/active"
export ACTIVE_MARK="$TMP/active"
OUT="$("$SERVICE" status 2>&1)"
echo "$OUT" | grep -q "systemd: active" || fail "status 未显示 systemd active"
echo "$OUT" | grep -q "temp: running" || fail "status 未显示 temp running"
echo "$OUT" | grep -q "ollama: healthy" || fail "status 未显示 ollama healthy"
kill "$FAKE_TEMP_PID" 2>/dev/null || true
rm -f "$TMP/pid/bridge-temp.pid"
unset ACTIVE_MARK
pass "status 三态展示"

# ── 用例 9: stop 停 systemd + 杀 temp ──
sleep 300 & FAKE_TEMP_PID=$!
echo "$FAKE_TEMP_PID" > "$TMP/pid/bridge-temp.pid"
touch "$TMP/active"
export ACTIVE_MARK="$TMP/active"
: > "$CALLS_LOG"
"$SERVICE" stop >/dev/null 2>&1 || fail "stop 退出非 0"
grep -q "stop chiguo-bridge" "$CALLS_LOG" || fail "stop 未停 systemd"
[ ! -f "$TMP/pid/bridge-temp.pid" ] || fail "stop 未清 temp pidfile"
kill "$FAKE_TEMP_PID" 2>/dev/null || true
unset ACTIVE_MARK
pass "stop 双侧停止"

# ── 用例 10: uninstall 删 unit + daemon-reload，登录态保留 ──
mkdir -p "$TMP/home/auth/wechat"
printf '{"token":"keep-me"}' > "$TMP/home/auth/wechat/credentials.json"
[ -f "$TMP/systemd/chiguo-bridge.service" ] || { "$SERVICE" autostart >/dev/null 2>&1 || true; }
: > "$CALLS_LOG"
"$SERVICE" uninstall >/dev/null 2>&1 || fail "uninstall 退出非 0"
[ ! -f "$TMP/systemd/chiguo-bridge.service" ] || fail "uninstall 未删 unit"
grep -q "daemon-reload" "$CALLS_LOG" || fail "uninstall 未 daemon-reload"
[ -f "$TMP/home/auth/wechat/credentials.json" ] || fail "uninstall 误删登录态"
pass "uninstall 清理 + 登录态保留"

# ── 用例 11: 缺 node → autostart 退出 2（CHIGUO_NODE 注入为空）──
mkdir -p "$TMP/nonode"
cat > "$TMP/nonode/curl" <<'STUB'
#!/usr/bin/env bash
exit 1
STUB
chmod +x "$TMP/nonode/curl"
cp "$TMP/bin/systemctl" "$TMP/nonode/systemctl"
PATH="$TMP/nonode:$PATH" CHIGUO_NODE="" bash "$SERVICE" autostart >/dev/null 2>&1
[ $? = 2 ] || fail "缺 node 应退出 2（实际 $?）"
pass "缺 node → 2"

# ── 用例 12: 缺 .env → autostart 退出 1 ──
rm -f "$TMP/repo/wechat-bridge/.env"
OUT="$("$SERVICE" autostart 2>&1)"
[ $? = 1 ] || fail "缺 .env 应退出 1（实际 $?）"
echo "$OUT" | grep -q "wechat-bridge.sh install" || fail "缺 .env 未提示 install"
touch "$TMP/repo/wechat-bridge/.env"
pass "缺 .env → 1 + 提示 install"

# ── 用例 13: 非 root → autostart 退出 2（PATH 前缀 fake id 输出 1000）──
mkdir -p "$TMP/nonroot"
cp "$TMP/bin/systemctl" "$TMP/nonroot/systemctl"
cp "$TMP/bin/curl" "$TMP/nonroot/curl"
cp "$TMP/bin/node" "$TMP/nonroot/node"
cat > "$TMP/nonroot/id" <<'STUB'
#!/usr/bin/env bash
echo "1000"
STUB
chmod +x "$TMP/nonroot/id"
PATH="$TMP/nonroot:$PATH" "$SERVICE" autostart >/dev/null 2>&1
[ $? = 2 ] || fail "非 root autostart 应退出 2（实际 $?）"
pass "非 root autostart → 2"
