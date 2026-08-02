#!/usr/bin/env bash
# netease-api.sh 桩测试：假 git/node/npm/systemctl/curl/id + 临时目录，验证 install 幂等/版本检查/
# unit 写入/status 退出码（4 用例）
set -euo pipefail
TMP="$(mktemp -d /tmp/chiguo-netease-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

mkdir -p "$TMP/bin" "$TMP/api" "$TMP/unit" "$TMP/home"
export HOME="$TMP/home"

cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$GIT_LOG"
if [ "$1" = clone ]; then
  DEST="${@: -1}"
  mkdir -p "$DEST/.git"
  echo '{"name":"@neteasecloudmusicapienhanced/api","version":"4.39.0"}' > "$DEST/package.json"
fi
STUB
cat > "$TMP/bin/node" <<'STUB'
#!/usr/bin/env bash
for a in "$@"; do
  if [[ "$a" == *package.json* ]]; then
    P="$(printf '%s' "$a" | grep -o "[^']*package\.json" | head -1)"
    sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$P" | head -1
    exit 0
  fi
done
exit 0
STUB
cat > "$TMP/bin/npm" <<'STUB'
#!/usr/bin/env bash
echo "npm $*" >> "$NPM_LOG"
mkdir -p "$(pwd)/node_modules"
STUB
cat > "$TMP/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$SYS_LOG"
if [ "$1" = is-active ]; then
  [ -n "${FAKE_ACTIVE:-}" ] && echo active && exit 0
  echo inactive
fi
exit 0
STUB
cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "$CURL_LOG"
if [ -n "${FAKE_CURL_FAIL_FILE:-}" ] && [ -f "$FAKE_CURL_FAIL_FILE" ]; then
  N=$(cat "$FAKE_CURL_FAIL_FILE")
  if [ "$N" -gt 0 ]; then
    echo "$((N - 1))" > "$FAKE_CURL_FAIL_FILE"
    echo '{"code":500}'
    exit 0
  fi
fi
case "$*" in
  *login/status*) echo '{"code":200,"data":{}}' ;;
  *) echo '{}' ;;
esac
STUB
cat > "$TMP/bin/id" <<'STUB'
#!/usr/bin/env bash
[ "$1" = -u ] && echo "${FAKE_UID:-0}" && exit 0
/usr/bin/id "$@"
STUB
for t in git node npm systemctl curl id; do chmod +x "$TMP/bin/$t"; done

export GIT_LOG="$TMP/git.log" NPM_LOG="$TMP/npm.log" SYS_LOG="$TMP/sys.log" CURL_LOG="$TMP/curl.log"
export PATH="$TMP/bin:$PATH"
export NETEASE_API_DIR="$TMP/api" NETEASE_API_UNIT="$TMP/unit/netease-api.service"

# ── 用例 1: 非 root → install 退出 1 且不 clone ──
export FAKE_UID=1000
set +e; bash scripts/netease-api.sh install >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] || fail "非 root 期望退出 1 实得 $RC"
[ ! -s "$GIT_LOG" ] || fail "非 root 不应 clone"
pass "非 root → 退出 1 且不 clone"

# ── 用例 2: 首次 install → clone + npm install + unit 写入 + enable --now ──
unset FAKE_UID
set +e; bash scripts/netease-api.sh install >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "install 期望 0 实得 $RC"
grep -q "git clone --depth 1 --branch v4.39.0" "$GIT_LOG" || fail "未按预期 clone（--branch v4.39.0）"
grep -q "npm install --no-fund --no-audit" "$NPM_LOG" || fail "npm install 参数不对"
[ -d "$TMP/api/node_modules" ] || fail "node_modules 未生成"
[ -f "$TMP/unit/netease-api.service" ] || fail "systemd unit 未写入"
grep -q "ExecStart=/usr/bin/env node app.js" "$TMP/unit/netease-api.service" || fail "unit ExecStart 不对"
grep -q "Restart=always" "$TMP/unit/netease-api.service" || fail "unit 缺 Restart=always"
grep -q "systemctl enable --now netease-api" "$SYS_LOG" || fail "未 enable --now"
grep -q "systemctl daemon-reload" "$SYS_LOG" || fail "未 daemon-reload"
pass "首次 install：clone + npm + unit + enable --now"

# ── 用例 3: 重跑 install 幂等 → 不重复 clone ──
: > "$GIT_LOG"
set +e; bash scripts/netease-api.sh install >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "幂等重跑期望 0 实得 $RC"
[ ! -s "$GIT_LOG" ] || fail "版本匹配不应再 clone"
pass "重跑 install 幂等：版本匹配跳过 clone"

# ── 用例 4: status 服务未运行 → 退出 1 ──
unset FAKE_ACTIVE
set +e; bash scripts/netease-api.sh status >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] || fail "status 未运行期望 1 实得 $RC"
export FAKE_ACTIVE=1
set +e; bash scripts/netease-api.sh status >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "status 运行中期望 0 实得 $RC"
pass "status 退出码：未运行 1 / 运行中 0"

# ── 用例 5: 健康检查首次失败 → 重试退避后成功 ──
: > "$CURL_LOG"
echo 2 > "$TMP/failcnt"
export FAKE_CURL_FAIL_FILE="$TMP/failcnt"
set +e; bash scripts/netease-api.sh install >/dev/null 2>&1; RC=$?; set -e
unset FAKE_CURL_FAIL_FILE
[ "$RC" = 0 ] || fail "健康重试期望 0 实得 $RC"
[ "$(wc -l < "$CURL_LOG")" -ge 3 ] || fail "应重试 ≥3 次 curl，实得 $(wc -l < "$CURL_LOG") 次"
pass "健康检查重试：首次失败后重试成功"

# ── 用例 6: start 同样带启动竞态重试 ──
: > "$CURL_LOG"
echo 2 > "$TMP/failcnt"
export FAKE_CURL_FAIL_FILE="$TMP/failcnt"
set +e; bash scripts/netease-api.sh start >/dev/null 2>&1; RC=$?; set -e
unset FAKE_CURL_FAIL_FILE
[ "$RC" = 0 ] || fail "start 重试期望 0 实得 $RC"
[ "$(wc -l < "$CURL_LOG")" -ge 3 ] || fail "start 应重试 ≥3 次 curl，实得 $(wc -l < "$CURL_LOG") 次"
pass "start 重试：首次失败后重试成功"
