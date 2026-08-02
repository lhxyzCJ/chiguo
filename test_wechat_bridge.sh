#!/usr/bin/env bash
# wechat-bridge.sh 桩测试：假 git/npm/pgrep + 临时仓库根，验证 install/start/status 行为与 .env 生成（9 用例）
set -euo pipefail
TMP="$(mktemp -d /tmp/chiguo-bridge-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

# ── 假工具：git（记录 clone/pull）、npm（记录 install、造 node_modules）、pgrep/pkill（模拟进程）──
mkdir -p "$TMP/bin" "$TMP/repo/.venv/bin" "$TMP/repo/wechat-bridge"
cat > "$TMP/repo/chiguo_proactive.toml" <<'TOML'
[openclaw]
wechat_recipient = "owner_test@im.wechat"
TOML
touch "$TMP/repo/.venv/bin/python" && chmod +x "$TMP/repo/.venv/bin/python"

cat > "$TMP/bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$GIT_LOG"
if [ "$1" = clone ]; then DEST="${@: -1}"; mkdir -p "$DEST/nodejs" "$DEST/.git"; echo "clone done" > "$DEST/.cloned"; fi
STUB
cat > "$TMP/bin/npm" <<'STUB'
#!/usr/bin/env bash
echo "npm $*" >> "$NPM_LOG"
mkdir -p "$(pwd)/node_modules/@wechatbot"
STUB
cat > "$TMP/bin/pgrep" <<'STUB'
#!/usr/bin/env bash
[ -n "${FAKE_PID:-}" ] && echo "$FAKE_PID" && exit 0
exit 1
STUB
cat > "$TMP/bin/pkill" <<'STUB'
#!/usr/bin/env bash
echo "pkill $*" >> "$PKILL_LOG"
exit 0
STUB
for t in git npm pgrep pkill bash sleep; do chmod +x "$TMP/bin/$t" 2>/dev/null || true; done

export GIT_LOG="$TMP/git.log" NPM_LOG="$TMP/npm.log" PKILL_LOG="$TMP/pkill.log"
export PATH="$TMP/bin:$PATH" HOME="$TMP/home"
export CHIGUO_REPO_OVERRIDE="$TMP/repo" WECHATBOT_DIR="$TMP/wechatbot" WECHAT_BRIDGE_LOG="$TMP/bridge.log"

# ── 用例 1: 缺 .venv → install 失败退出 2 ──
rm -rf "$TMP/repo/.venv"
set +e; bash scripts/wechat-bridge.sh install >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 2 ] && pass "缺 .venv → 退出 2" || fail "缺 .venv 期望 2 实得 $RC"
mkdir -p "$TMP/repo/.venv/bin" && touch "$TMP/repo/.venv/bin/python" && chmod +x "$TMP/repo/.venv/bin/python"

# ── 用例 2: 首次 install → clone wechatbot + npm install file: + .env 生成 ──
set +e; bash scripts/wechat-bridge.sh install >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "install 期望 0 实得 $RC"
grep -q "git clone --depth 1 https://github.com/lhxyzCJ/wechatbot.git $TMP/wechatbot" "$GIT_LOG" || fail "未按预期 clone wechatbot"
grep -q "npm install @wechatbot/wechatbot@file:$TMP/wechatbot/nodejs" "$NPM_LOG" || fail "npm install file: 参数不对"
[ -d "$TMP/repo/wechat-bridge/node_modules/@wechatbot" ] || fail "node_modules 未生成"
grep -q "WECHAT_BRIDGE_OWNER=owner_test@im.wechat" "$TMP/repo/wechat-bridge/.env" || fail ".env 未从 toml 读 OWNER"
grep -q "WECHAT_BRIDGE_STORAGE=$TMP/repo/wechat-bridge/credentials" "$TMP/repo/wechat-bridge/.env" || fail ".env STORAGE 路径不对"
pass "首次 install：clone + npm file: + .env 生成"

# ── 用例 3: 重跑 install 幂等 → 不重复 clone（走 pull）──
: > "$GIT_LOG"
set +e; bash scripts/wechat-bridge.sh install >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "重跑 install 期望 0 实得 $RC"
grep -q "git clone" "$GIT_LOG" && fail "重复 clone（应走 pull）" || true
grep -q "git -C $TMP/wechatbot pull" "$GIT_LOG" || fail "已存在时应 pull 更新"
pass "重跑 install：不重复 clone"

# ── 用例 4: 无登录态 → status 退出 1 且提示扫码 ──
set +e; OUT=$(bash scripts/wechat-bridge.sh status 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "无凭证 status 期望 1 实得 $RC"
echo "$OUT" | grep -q "无登录态" || fail "缺少无登录态提示"
pass "无登录态 status → 退出 1"

# ── 用例 5: 有登录态 → status 退出 0 且显示账号 ──
mkdir -p "$TMP/repo/wechat-bridge/credentials"
printf '{"userId":"owner_test@im.wechat"}' > "$TMP/repo/wechat-bridge/credentials/credentials.json"
set +e; OUT=$(FAKE_PID=123 bash scripts/wechat-bridge.sh status 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "有凭证 status 期望 0 实得 $RC"
echo "$OUT" | grep -q "owner_test@im.wechat" || fail "status 未显示账号"
pass "有登录态 status → 退出 0 + 账号"

# ── 用例 6: start 缺 .env → 提示安装返回 1 ──
rm -f "$TMP/repo/wechat-bridge/.env"
set +e; OUT=$(bash scripts/wechat-bridge.sh start 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "缺 .env start 期望 1 实得 $RC"
echo "$OUT" | grep -q "install" || fail "缺 .env 提示未指向 install"
pass "start 缺 .env → 提示 install 返回 1"

# ── 用例 7: login → 清除凭证 + 调用 stop（桩无法模拟杀后进程消失，扫码提示属于 start 无凭证分支）──
bash scripts/wechat-bridge.sh install >/dev/null 2>&1
: > "$PKILL_LOG"
set +e; OUT=$(FAKE_PID=123 bash scripts/wechat-bridge.sh login 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "login 期望 0 实得 $RC（$OUT）"
[ -f "$TMP/repo/wechat-bridge/credentials/credentials.json" ] && fail "login 未清除凭证" || true
grep -q "pkill" "$PKILL_LOG" || fail "login 未调用 stop"
pass "login：清凭证 + stop"

# ── 用例 8: 已有进程 → start 不重复启动 ──
printf '{"userId":"u"}' > "$TMP/repo/wechat-bridge/credentials/credentials.json"
: > "$NPM_LOG"
set +e; OUT=$(FAKE_PID=999 bash scripts/wechat-bridge.sh start 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "已运行 start 期望 0 实得 $RC"
echo "$OUT" | grep -q "已在运行" || fail "start 未识别已运行状态"
pass "已运行 start → 幂等提示"

# ── 用例 9: OPENCODE_API_KEY 注入——优先 opencode-go 条目，无则回退 [host].provider ──
cat > "$TMP/repo/chiguo_proactive.toml" <<'TOML'
[host]
wechat_recipient = "owner_test@im.wechat"
provider = "deepseek"
TOML
mkdir -p "$TMP/home/.pi/agent"
printf '{"opencode-go":{"type":"api_key","key":"sk-og"},"deepseek":{"type":"api_key","key":"sk-ds"}}' \
  > "$TMP/home/.pi/agent/auth.json"
set +e; OUT=$(HOME="$TMP/home" bash scripts/wechat-bridge.sh install 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "provider .env 生成期望 0 实得 $RC"
grep -q "OPENCODE_API_KEY=sk-og" "$TMP/repo/wechat-bridge/.env" \
  || fail ".env 应优先注入 opencode-go key: $(cat "$TMP/repo/wechat-bridge/.env")"
# 无 opencode-go 条目 → 回退 [host].provider=deepseek 条目
printf '{"deepseek":{"type":"api_key","key":"sk-ds"}}' > "$TMP/home/.pi/agent/auth.json"
set +e; OUT=$(HOME="$TMP/home" bash scripts/wechat-bridge.sh install 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "回退 .env 生成期望 0 实得 $RC"
grep -q "OPENCODE_API_KEY=sk-ds" "$TMP/repo/wechat-bridge/.env" \
  || fail ".env 未回退注入 deepseek key: $(cat "$TMP/repo/wechat-bridge/.env")"
pass "OPENCODE_API_KEY 注入：优先 opencode-go、回退 [host].provider"

echo "test_wechat_bridge: 通过"
