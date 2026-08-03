#!/usr/bin/env bash
# test_tick_health.sh — chiguo-tick.sh 的 pi 假死记账 + 告警/恢复链路测试（独立 runner）
# 用法: bash test_tick_health.sh（退出码 0=全过，1=有失败）
# 隔离: temp repo（fake daemon 输出 send + fake pi-run 可切换成败 + 真 pi_health.py 拷贝
# + node http 记录服务当 bridge /send）；绝不碰真实仓库状态。
set -euo pipefail
TMP="$(mktemp -d /tmp/chiguo-tick-health.XXXXXX)"
trap 'kill ${SRV_PID:-} 2>/dev/null || true; rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

REAL_TICK="/root/chiguo/scripts/chiguo-tick.sh"
REPO="$TMP/repo"
mkdir -p "$REPO/scripts" "$REPO/.venv/bin"
ln -s /root/chiguo/.venv/bin/python "$REPO/.venv/bin/python"

# ── pi-auth.sh 共同 sourcing(opencode-go 优先 → toml provider 回退)──
pass "pi-auth: sourcing sets OPENCODE_API_KEY from auth.json"
test_pi_auth() {
  local AUTH_DIR="$TMP/pi-auth"
  mkdir -p "$AUTH_DIR"
  mkdir -p "$REPO/scripts"
  ln -sf /root/chiguo/scripts/pi-auth.sh "$REPO/scripts/pi-auth.sh" 2>/dev/null || \
    cp /root/chiguo/scripts/pi-auth.sh "$REPO/scripts/pi-auth.sh"
  # 用例 1:opencode-go 优先
  mkdir -p "$AUTH_DIR/.pi/agent"
  cat > "$AUTH_DIR/.pi/agent/auth.json" <<'JSON'
{"opencode-go": {"key": "KEY_OG"}, "local": {"key": "KEY_LOCAL"}}
JSON
  cat > "$REPO/chiguo_proactive.toml" <<TOML
[host]
provider = "local"
TOML
  local KEY
  KEY="$(HOME="$AUTH_DIR" CHIGUO_REPO="$REPO" bash -c 'source "$CHIGUO_REPO/scripts/pi-auth.sh"; printf "%s" "${OPENCODE_API_KEY:-}"' 2>/dev/null || true)"
  [ "$KEY" = "KEY_OG" ] || fail "pi-auth: 期望 opencode-go 优先, got '$KEY'"
  # 用例 2:无 opencode-go → toml provider 回退
  cat > "$AUTH_DIR/.pi/agent/auth.json" <<'JSON'
{"local": {"key": "KEY_LOCAL"}}
JSON
  KEY="$(HOME="$AUTH_DIR" CHIGUO_REPO="$REPO" bash -c 'source "$CHIGUO_REPO/scripts/pi-auth.sh"; printf "%s" "${OPENCODE_API_KEY:-}"' 2>/dev/null || true)"
  [ "$KEY" = "KEY_LOCAL" ] || fail "pi-auth: 期望 toml provider 回退, got '$KEY'"
  # 用例 3:无 auth.json → 空串不报错
  rm -rf "$AUTH_DIR/.pi"
  KEY="$(HOME="$AUTH_DIR" CHIGUO_REPO="$REPO" bash -c 'source "$CHIGUO_REPO/scripts/pi-auth.sh"; printf "%s" "${OPENCODE_API_KEY:-}"' 2>/dev/null || true)"
  [ -z "$KEY" ] || fail "pi-auth: 期望空 key, got '$KEY'"
  pass "pi-auth: sourcing sets OPENCODE_API_KEY from auth.json"
}
test_pi_auth

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
POST_LOG="$TMP/post.log"
cat > "$TMP/recorder.js" <<'JS'
const http = require('http')
const fs = require('fs')
http.createServer((req, res) => {
  let b = ''
  req.on('data', (c) => { b += c })
  req.on('end', () => { fs.appendFileSync(process.argv[2], b + '\n'); res.end('{"ok":true}') })
}).listen(Number(process.argv[3]), '127.0.0.1')
JS
node "$TMP/recorder.js" "$POST_LOG" "$PORT" &
SRV_PID=$!

cat > "$REPO/chiguo_proactive.toml" <<TOML
[host]
send_session_id = "chiguo-send"
wechat_bridge_url = "http://127.0.0.1:$PORT/send"

[wechat]
wechat_recipient = "owner_test@im.wechat"

[health]
fail_threshold = 3
TOML

cat > "$REPO/chiguo_daemon.py" <<'PY'
import json, sys
if "--compact" in sys.argv:
    print(json.dumps({"action": "send", "msg_id": "abc123", "trigger": "lonely_mid", "context": {}}))
else:
    sys.exit(0)
PY

cat > "$REPO/scripts/pi-run.mjs" <<'JS'
import { readFileSync } from 'node:fs'
const mode = readFileSync(process.env.FAKE_PI_MODE_FILE, 'utf8').trim()
if (mode === 'success') {
  process.stdout.write(JSON.stringify({ ok: true, text: '测试主动消息' }))
} else {
  process.stdout.write(JSON.stringify({ ok: false, error: 'tick 模拟故障' }))
}
JS

cp /root/chiguo/scripts/pi_health.py "$REPO/scripts/pi_health.py"
export FAKE_PI_MODE_FILE="$TMP/pi_mode"
echo fail > "$FAKE_PI_MODE_FILE"

STATE="$REPO/pi_health.json"
post_count() { [ -f "$POST_LOG" ] && wc -l < "$POST_LOG" || echo 0; }
state_field() { python3 -c "import json; print(json.load(open('$STATE')).get('$1',''))" 2>/dev/null || echo ''; }
post_texts() { python3 -c "
import json
for line in open('$POST_LOG'):
    try: print(json.loads(line).get('text',''))
    except Exception: pass" 2>/dev/null; }

# ── 用例 1: 单次失败 → 记账但未达阈值，无告警；tick 仍退出 1 ──
set +e
CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1
RC=$?
set -e
[ "$RC" = 1 ] && pass "pi 失败时 tick 退出 1（既有语义保留）" || fail "退出码期望 1 实得 $RC"
[ -f "$STATE" ] || fail "pi_health.json 未创建（未记账）"
[ "$(state_field state)" = up ] || fail "state 期望 up 实得 $(state_field state)"
[ "$(state_field fail_streak)" = 1 ] || fail "fail_streak 期望 1 实得 $(state_field fail_streak)"
[ "$(post_count)" = 0 ] && pass "未达阈值 → 零告警 POST" || fail "期望 0 POST 实得 $(post_count)"

# ── 用例 2: 累计 3 次失败 → state=down + 恰好 1 条告警 POST（含次数与原因）──
for i in 2 3; do
  set +e; CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
  [ "$RC" = 1 ] || fail "第 $i 次失败应退出 1"
done
[ "$(state_field state)" = down ] || fail "state 期望 down 实得 $(state_field state)"
[ "$(state_field fail_streak)" = 3 ] || fail "fail_streak 期望 3 实得 $(state_field fail_streak)"
[ "$(post_count)" = 1 ] && pass "越过阈值 → 恰好 1 条告警" || fail "期望 1 POST 实得 $(post_count)"
post_texts | grep -q "3" || fail "告警应含失败次数"
post_texts | grep -q "tick 模拟故障" || fail "告警应含失败原因"

# ── 用例 3: 已 down 再失败 → 不重复告警 ──
set +e; CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; set -e
[ "$(post_count)" = 1 ] && pass "已 down 再失败 → 不重复告警" || fail "期望仍 1 POST 实得 $(post_count)"

# ── 用例 4: 恢复 → 真实消息 + 恢复通知都发出 ──
echo success > "$FAKE_PI_MODE_FILE"
CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1 || fail "成功路径 tick 应退出 0"
[ "$(state_field state)" = up ] || fail "state 期望 up 实得 $(state_field state)"
[ "$(post_count)" = 3 ] && pass "恢复后收到 真实消息 + 恢复通知" || fail "期望 3 POST（告警1+消息1+恢复1）实得 $(post_count)"
post_texts | grep -q "测试主动消息" || fail "应发出真实主动消息"
post_texts | grep -q "恢复" || fail "应发出恢复通知"

# ── 用例 5: OPENCODE_API_KEY 注入——优先 opencode-go 条目（memory 扩展端点固定），无则回退 [host].provider ──
mkdir -p "$TMP/home/.pi/agent"
printf '{"opencode-go":{"type":"api_key","key":"sk-og"},"deepseek":{"type":"api_key","key":"sk-ds"}}' \
  > "$TMP/home/.pi/agent/auth.json"
cat > "$REPO/chiguo_proactive.toml" <<TOML
[host]
send_session_id = "chiguo-send"
wechat_bridge_url = "http://127.0.0.1:$PORT/send"
provider = "deepseek"
model = "deepseek-chat"

[wechat]
wechat_recipient = "owner_test@im.wechat"

[health]
fail_threshold = 3
TOML
cat > "$REPO/scripts/pi-run.mjs" <<'JS'
import { readFileSync, appendFileSync } from 'node:fs'
appendFileSync(process.env.KEY_LOG, 'KEY=' + (process.env.OPENCODE_API_KEY || '') + '\n')
const mode = readFileSync(process.env.FAKE_PI_MODE_FILE, 'utf8').trim()
if (mode === 'success') {
  process.stdout.write(JSON.stringify({ ok: true, text: '测试主动消息' }))
} else {
  process.stdout.write(JSON.stringify({ ok: false, error: 'tick 模拟故障' }))
}
JS
export KEY_LOG="$TMP/key.log"
: > "$KEY_LOG"
echo success > "$FAKE_PI_MODE_FILE"
HOME="$TMP/home" CHIGUO_REPO="$REPO" env -u OPENCODE_API_KEY bash "$REAL_TICK" >/dev/null 2>&1 \
  || fail "provider 用例 tick 应退出 0"
grep -q "KEY=sk-og" "$KEY_LOG" || fail "应优先注入 opencode-go 条目: $(cat "$KEY_LOG")"
# 无 opencode-go 条目 → 回退 [host].provider=deepseek 条目
printf '{"deepseek":{"type":"api_key","key":"sk-ds"}}' > "$TMP/home/.pi/agent/auth.json"
: > "$KEY_LOG"
HOME="$TMP/home" CHIGUO_REPO="$REPO" env -u OPENCODE_API_KEY bash "$REAL_TICK" >/dev/null 2>&1 \
  || fail "回退用例 tick 应退出 0"
grep -q "KEY=sk-ds" "$KEY_LOG" || fail "无 opencode-go 条目时应回退 deepseek key: $(cat "$KEY_LOG")"
pass "OPENCODE_API_KEY 注入：优先 opencode-go、回退 [host].provider"

# ── 用例 6: 登录后收件人注入——~/.chiguo/auth/wechat/credentials.json 的 userId 生效 ──
cat > "$REPO/chiguo_proactive.toml" <<TOML
[host]
send_session_id = "chiguo-send"
wechat_bridge_url = "http://127.0.0.1:$PORT/send"
provider = "deepseek"
model = "deepseek-chat"

[wechat]
wechat_recipient = "owner@im.wechat"

[health]
fail_threshold = 3
TOML
mkdir -p "$TMP/home/.chiguo/auth/wechat"
printf '{"token":"t","userId":"real_openid@im.wechat","accountId":"a"}' > "$TMP/home/.chiguo/auth/wechat/credentials.json"
: > "$POST_LOG"
echo success > "$FAKE_PI_MODE_FILE"
HOME="$TMP/home" CHIGUO_REPO="$REPO" env -u OPENCODE_API_KEY bash "$REAL_TICK" >/dev/null 2>&1 \
  || fail "收件人注入用例 tick 应退出 0"
grep -q "real_openid@im.wechat" "$POST_LOG" || fail "主动消息应发往真实 userId: $(cat "$POST_LOG")"
grep -q '"to": "owner@im.wechat"' "$POST_LOG" && fail "不应发往占位符" || true
pass "登录后收件人自动注入（credentials userId 生效）"

echo "test_tick_health: 全部通过"
