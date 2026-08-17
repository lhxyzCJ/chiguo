#!/usr/bin/env bash
# test_tick_health.sh — chiguo-tick.sh 的 agent 假死记账 + 告警/恢复链路测试（独立 runner）
# 用法: bash test_tick_health.sh（退出码 0=全过，1=有失败）
# 隔离: temp repo（fake daemon 输出 send + fake agent-run 可切换成败 + 真 agent_health.py 拷贝
# + node http 记录服务当 bridge /send）；绝不碰真实仓库状态。
set -euo pipefail
TMP="$(mktemp -d /tmp/chiguo-tick-health.XXXXXX)"
trap 'kill ${SRV_PID:-} 2>/dev/null || true; rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

REPO_ROOT="${CHIGUO_REPO_OVERRIDE:-$(cd "$(dirname "$0")/.." && pwd)}"
REAL_TICK="$REPO_ROOT/scripts/chiguo-tick.sh"
REPO="$TMP/repo"
mkdir -p "$REPO/scripts" "$REPO/.venv/bin"
ln -s "$REPO_ROOT/.venv/bin/python" "$REPO/.venv/bin/python"

# ── agent-auth.sh 共同 sourcing(opencode-go 优先 → toml provider 回退)──
pass "agent-auth: sourcing sets OPENCODE_API_KEY from auth.json"
test_agent_auth() {
  local AUTH_DIR="$TMP/agent-auth"
  mkdir -p "$AUTH_DIR"
  mkdir -p "$REPO/scripts"
  ln -sf "$REPO_ROOT/scripts/agent-auth.sh" "$REPO/scripts/agent-auth.sh" 2>/dev/null || \
    cp "$REPO_ROOT/scripts/agent-auth.sh" "$REPO/scripts/agent-auth.sh"
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
  KEY="$(HOME="$AUTH_DIR" CHIGUO_REPO="$REPO" bash -c 'source "$CHIGUO_REPO/scripts/agent-auth.sh"; printf "%s" "${OPENCODE_API_KEY:-}"' 2>/dev/null || true)"
  [ "$KEY" = "KEY_OG" ] || fail "agent-auth: 期望 opencode-go 优先, got '$KEY'"
  # 用例 2:无 opencode-go → toml provider 回退
  cat > "$AUTH_DIR/.pi/agent/auth.json" <<'JSON'
{"local": {"key": "KEY_LOCAL"}}
JSON
  KEY="$(HOME="$AUTH_DIR" CHIGUO_REPO="$REPO" bash -c 'source "$CHIGUO_REPO/scripts/agent-auth.sh"; printf "%s" "${OPENCODE_API_KEY:-}"' 2>/dev/null || true)"
  [ "$KEY" = "KEY_LOCAL" ] || fail "agent-auth: 期望 toml provider 回退, got '$KEY'"
  # 用例 3:无 auth.json → 空串不报错
  rm -rf "$AUTH_DIR/.pi"
  KEY="$(HOME="$AUTH_DIR" CHIGUO_REPO="$REPO" bash -c 'source "$CHIGUO_REPO/scripts/agent-auth.sh"; printf "%s" "${OPENCODE_API_KEY:-}"' 2>/dev/null || true)"
  [ -z "$KEY" ] || fail "agent-auth: 期望空 key, got '$KEY'"
  pass "agent-auth: sourcing sets OPENCODE_API_KEY from auth.json"
}
test_agent_auth

PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
POST_LOG="$TMP/post.log"
cat > "$TMP/recorder.js" <<'JS'
const http = require('http')
const fs = require('fs')
const rpcModeFile = process.argv[4]
const sendModeFile = process.argv[5]
http.createServer((req, res) => {
  let b = ''
  req.on('data', (c) => { b += c })
  req.on('end', () => {
    fs.appendFileSync(process.argv[2], JSON.stringify({ url: req.url, body: b }) + '\n')
    if (req.url === '/agent/prompt') {
      let rpc = ''
      try { rpc = fs.readFileSync(rpcModeFile, 'utf8').trim() } catch {}
      const text = rpc === 'success' ? JSON.stringify({ ok: true, text: 'RPC 主动消息' })
        : rpc === 'queue_busy' ? JSON.stringify({ ok: false, error: 'queue_busy: RPC send 排队超时' })
        : JSON.stringify({ ok: false, error: 'mock RPC 故障' })
      res.end(text)
    } else {
      let sendMode = 'ok'
      try { sendMode = fs.readFileSync(sendModeFile, 'utf8').trim() } catch {}
      if (sendMode === 'timeout_uncertain') {
        res.end(JSON.stringify({ ok: false, error: 'timeout 30000ms', timeout_uncertain: true }))
      } else if (sendMode === 'doconnect_explicit_fail') {
        res.end(JSON.stringify({ ok: false, error: 'mock bridge 明确失败' }))
      } else if (sendMode === 'non_json') {
        // RF1 (M6-2): 网关/代理 200 + 非预期响应体（HTML 错误页/包装）——不是 JSON。
        res.end('<html><body>502 Bad Gateway</body></html>')
      } else {
        res.end('{"ok":true}')
      }
    }
  })
}).listen(Number(process.argv[3]), '127.0.0.1')
JS
node "$TMP/recorder.js" "$POST_LOG" "$PORT" "$TMP/rpc_mode" "$TMP/send_mode" &
SRV_PID=$!
RPC_MODE="$TMP/rpc_mode"
echo fail > "$RPC_MODE"
SEND_MODE="$TMP/send_mode"
echo ok > "$SEND_MODE"

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
import json, sys, os
if "--compact" in sys.argv:
    with open(os.environ.get("COMPACT_LOG", "/dev/null"), "a") as f:
        f.write("compact\n")
    if os.environ.get("FAKE_DAEMON_ACTION") == "idle":
        print(json.dumps({"action": "idle"}))
    else:
        print(json.dumps({"action": "send", "msg_id": "abc123", "trigger": "lonely_mid", "context": {}}))
elif "--send-result" in sys.argv:
    with open(os.environ.get("SEND_RESULT_LOG", "/dev/null"), "a") as f:
        f.write(json.dumps(sys.argv) + "\n")
else:
    sys.exit(0)
PY

cat > "$REPO/scripts/agent-run.mjs" <<'JS'
import { readFileSync, appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_AGENT_CALLS, 'x')
const mode = readFileSync(process.env.FAKE_AGENT_MODE_FILE, 'utf8').trim()
if (mode === 'success') {
  process.stdout.write(JSON.stringify({ ok: true, text: '测试主动消息' }))
} else {
  process.stdout.write(JSON.stringify({ ok: false, error: 'tick 模拟故障' }))
}
JS

export FAKE_AGENT_CALLS="$TMP/agent_calls"
: > "$FAKE_AGENT_CALLS"
spawn_count() { [ -f "$FAKE_AGENT_CALLS" ] && wc -c < "$FAKE_AGENT_CALLS" || echo 0; }
# #223 活动标记（轮换空闲保护）：隔离到 TMP，绝不写真实 ~/.chiguo
export CHIGUO_ACTIVITY_FILE="$TMP/activity"

cp "$REPO_ROOT/scripts/agent_health.py" "$REPO/scripts/agent_health.py"
export FAKE_AGENT_MODE_FILE="$TMP/agent_mode"
echo fail > "$FAKE_AGENT_MODE_FILE"

STATE="$REPO/agent_health.json"  # agent_health.py record 默认状态文件（#99：agent_health.json）
post_count() { python3 -c "
import json
n = 0
for line in open('$POST_LOG'):
    try:
        if json.loads(line).get('url') == '/send': n += 1
    except Exception: pass
print(n)" 2>/dev/null || echo 0; }
state_field() { python3 -c "import json; print(json.load(open('$STATE')).get('$1',''))" 2>/dev/null || echo ''; }
post_texts() { python3 -c "
import json
for line in open('$POST_LOG'):
    try:
        d = json.loads(line)
        if d.get('url') != '/send': continue
        print(json.loads(d.get('body','{}')).get('text',''))
    except Exception: pass" 2>/dev/null; }

# ── 用例 1: 单次失败 → 记账但未达阈值，无告警；tick 仍退出 1 ──
set +e
CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1
RC=$?
set -e
[ "$RC" = 1 ] && pass "agent 失败时 tick 退出 1（既有语义保留）" || fail "退出码期望 1 实得 $RC"
[ -f "$STATE" ] || fail "health 状态文件未创建（未记账）"
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
echo success > "$FAKE_AGENT_MODE_FILE"
CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1 || fail "成功路径 tick 应退出 0"
[ "$(state_field state)" = up ] || fail "state 期望 up 实得 $(state_field state)"
[ "$(post_count)" = 3 ] && pass "恢复后收到 真实消息 + 恢复通知" || fail "期望 3 POST（告警1+消息1+恢复1）实得 $(post_count)"
post_texts | grep -q "测试主动消息" || fail "应发出真实主动消息"
post_texts | grep -q "恢复" || fail "应发出恢复通知"

# ── 用例 4.5: U2 (#227) 去 composer 兜底——agent 尽整链重试失败 → 无兜底文本、
# 不发送、record fail（fail_streak 推进）+ exit 1 ──
# 与旧 A8 相反：不再 composer 模板池兜底出消息；失败即中止发送 + 记账。
echo fail > "$FAKE_AGENT_MODE_FILE"   # RPC 本就是 fail，spawn 也切 fail → 整链失败
POST_BEFORE="$(post_count)"
STREAK_BEFORE="$(state_field fail_streak)"
set +e; CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] || fail "去 composer 兜底时 agent 失败应 exit 1, 实得 $RC"
# 无兜底消息发出：POST 数不增（不发消息/兜底文本；未达阈值也无告警）
[ "$(post_count)" = "$POST_BEFORE" ]   && pass "去兜底: 未达阈值 agent 失败 → 无 POST（不兜底发送）" || fail "期望 POST=$POST_BEFORE 实得 $(post_count)"
# 记账推进 fail_streak
[ "$(state_field fail_streak)" -gt "$STREAK_BEFORE" ]   && pass "去兜底: fail 记账推进 fail_streak（$STREAK_BEFORE→$(state_field fail_streak)）"   || fail "fail_streak 应 > $STREAK_BEFORE, 实得 $(state_field fail_streak)"

# ── 用例 5: OPENCODE_API_KEY 注入——优先 opencode-go 条目（mem0 LLM 事实提取），无则回退 [host].provider ──
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
cat > "$REPO/scripts/agent-run.mjs" <<'JS'
import { readFileSync, appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_AGENT_CALLS, 'x')
appendFileSync(process.env.KEY_LOG, 'KEY=' + (process.env.OPENCODE_API_KEY || '') + '\n')
const mode = readFileSync(process.env.FAKE_AGENT_MODE_FILE, 'utf8').trim()
if (mode === 'success') {
  process.stdout.write(JSON.stringify({ ok: true, text: '测试主动消息' }))
} else {
  process.stdout.write(JSON.stringify({ ok: false, error: 'tick 模拟故障' }))
}
JS
export KEY_LOG="$TMP/key.log"
: > "$KEY_LOG"
echo success > "$FAKE_AGENT_MODE_FILE"
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
echo success > "$FAKE_AGENT_MODE_FILE"
HOME="$TMP/home" CHIGUO_REPO="$REPO" env -u OPENCODE_API_KEY bash "$REAL_TICK" >/dev/null 2>&1 \
  || fail "收件人注入用例 tick 应退出 0"
grep -q "real_openid@im.wechat" "$POST_LOG" || fail "主动消息应发往真实 userId: $(cat "$POST_LOG")"
grep -q '"to": "owner@im.wechat"' "$POST_LOG" && fail "不应发往占位符" || true
pass "登录后收件人自动注入（credentials userId 生效）"

# ── 用例 6b: 发送侧 RPC 优先——RPC success → 零 spawn；RPC fail → 回退 spawn ──
: > "$FAKE_AGENT_CALLS"
: > "$POST_LOG"
echo success > "$RPC_MODE"          # recorder /agent/prompt 返回 {ok:true,text:'RPC 主动消息'}
echo success > "$FAKE_AGENT_MODE_FILE"
HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1 \
  || fail "RPC 成功用例 tick 应退出 0"
[ "$(spawn_count)" = 0 ] && pass "RPC 成功 → 零 spawn agent-run" || fail "期望 0 spawn 实得 $(spawn_count)"
post_texts | grep -q "RPC 主动消息" && pass "RPC 生成的文本已发送" || fail "应发送 RPC 文本: $(post_texts)"
echo fail > "$RPC_MODE"             # recorder /agent/prompt 返回 {ok:false}
: > "$FAKE_AGENT_CALLS"
HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >"$TMP/tick6b.log" 2>&1 \
  || { cat "$TMP/tick6b.log" >&2 || true; fail "RPC 失败回退用例 tick 应退出 0"; }
  cat "$TMP/tick6b.log" >&2 || true
[ "$(spawn_count)" -ge 1 ] && pass "RPC 失败 → 回退 spawn agent-run" || fail "期望 spawn ≥1 实得 $(spawn_count)"
post_texts | grep -q "测试主动消息" && pass "回退 spawn 的文本已发送" || fail "应发送 spawn 文本"

# ── R10 (F-A17-004): RPC queue_busy（明确失败）与超时同归回退 spawn — 分流语义 ──
# tick 对 `/agent/prompt` 的**明确失败**（含 queue_busy：bridge 侧排队超预算快速判败）
# 与超时都走同一回退：spawn 立即接管，不并行等待、不放双 LLM 窗口。
# queue_busy 是 bridge 在预算内（≤ tick 125s）返回的确定失败，tick 收到即 spawn。
: > "$FAKE_AGENT_CALLS"
echo queue_busy > "$RPC_MODE"        # recorder /agent/prompt 返回 {ok:false,error:'queue_busy:...'}
echo success > "$FAKE_AGENT_MODE_FILE"
: > "$POST_LOG"                       # 隔离：只验本用例的发送记录
HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >"$TMP/tickR10.log" 2>&1 \
  || { cat "$TMP/tickR10.log" >&2 || true; fail "queue_busy 明确失败用例 tick 应退出 0"; }
[ "$(spawn_count)" -ge 1 ] && pass "queue_busy（明确失败）→ 立即回退 spawn" || fail "queue_busy 期望 spawn ≥1 实得 $(spawn_count)"
post_texts | grep -q "测试主动消息" && pass "queue_busy 回退 spawn 的文本已发送" || fail "应发送 spawn 文本: $(post_texts)"
post_texts | grep -q "RPC 主动消息" && fail "queue_busy 不应发送 RPC 文本（RPC 未产出）" || true
grep -q "RPC 未产出" "$TMP/tickR10.log" && pass "tick 日志记录 RPC 未产出（回退原因）" || fail "tick 应日志记录 RPC 回退: $(cat "$TMP/tickR10.log")"
echo fail > "$RPC_MODE"              # 复位 recorder

# ── R8 (F-A17-003): /send 超时返回 timeout_uncertain → 不退款、不记 send_fail、不重发 ──
# 修复前红：当作明确失败 → 回传 --send-result failed（退款）+ record_health send_fail。
# 超时不确定语义：消息可能已送达，退款恢复额度会制造重发窗口（重复消息）——
# 故 timeout_uncertain 不 refund、不 send_fail、本 tick 结束下轮自然再试（exit 0）。
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
echo success > "$FAKE_AGENT_MODE_FILE"
export SEND_RESULT_LOG="$TMP/sendresult.log"
rm -f "$SEND_RESULT_LOG"
: > "$SEND_RESULT_LOG"
echo timeout_uncertain > "$SEND_MODE"
STREAK_BEFORE="$(state_field fail_streak)"
set +e; HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "timeout_uncertain → tick 退出 0（本 tick 结束，下轮自然再试）" || fail "timeout_uncertain 应 exit 0, 实得 $RC"
# RF11: timeout_uncertain 不再完全无回传——做轻量清算（--send-status uncertain，只清未回复
# 计数），但**不得**是 refund（failed）。区分二者：log 含 uncertain 且不含 failed。
if grep -q 'uncertain' "$SEND_RESULT_LOG" && ! grep -q 'failed' "$SEND_RESULT_LOG"; then
  pass "timeout_uncertain → 回传 --send-result uncertain（轻量清算未回复，不 refund）"
else
  fail "timeout_uncertain 应回传 --send-result uncertain（非 failed）: $(cat "$SEND_RESULT_LOG")"
fi
[ "$(state_field fail_streak)" = "$STREAK_BEFORE" ] && pass "timeout_uncertain → 不记 send_fail（fail_streak 不变）" || fail "timeout_uncertain 不应推进 fail_streak（$STREAK_BEFORE→$(state_field fail_streak)）"
echo ok > "$SEND_MODE"

# ── RF1 (M6-2): 发送成功判定升级 JSON 解析——网关 200 + 非 JSON 响应体（HTML 错误页/
# 代理包装）时消息可能已送达，若记 send_fail 会误 down。故非 JSON 体 → 不确定：不退款、
# 不记 send_fail（fail_streak 不变）、不回传 --send-result failed、exit 0。──
export SEND_RESULT_LOG="$TMP/sendresult_rf1.log"
rm -f "$SEND_RESULT_LOG"
: > "$SEND_RESULT_LOG"
echo non_json > "$SEND_MODE"
STREAK_BEFORE="$(state_field fail_streak)"
set +e; HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "RF1: 非 JSON 响应体 → tick 退出 0（不确定，本 tick 结束）" || fail "RF1 非 JSON 体应 exit 0, 实得 $RC"
[ "$(state_field fail_streak)" = "$STREAK_BEFORE" ] \
  && pass "RF1: 非 JSON 响应体 → 不记 send_fail（fail_streak 不变）" \
  || fail "RF1 非 JSON 体不应推进 fail_streak（$STREAK_BEFORE→$(state_field fail_streak)）"
# RF1 非 JSON 体 = 结果不确定 → 亦做轻量清算（--send-status uncertain，非 failed 退款）
if grep -q 'uncertain' "$SEND_RESULT_LOG" && ! grep -q 'failed' "$SEND_RESULT_LOG"; then
  pass "RF1: 非 JSON 响应体 → 回传 --send-result uncertain（不清 refund）"
else
  fail "RF1 非 JSON 体应回传 --send-result uncertain（非 failed）: $(cat "$SEND_RESULT_LOG")"
fi
echo ok > "$SEND_MODE"


kill ${SRV_PID:-} 2>/dev/null || true
export SEND_RESULT_LOG="$TMP/sendresult.log"
: > "$SEND_RESULT_LOG"
set +e; HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] || fail "bridge 不可达 tick 应退出 1（发送失败，issue #85）"
grep -q "send-result.*failed" "$SEND_RESULT_LOG" \
  || fail "bridge 不可达应回传 --send-result failed: $(cat "$SEND_RESULT_LOG")"
# F-A6-2: 发送失败也进 health（send_fail 推进 fail_streak，不再恒 up）
[ "$(state_field state)" = up ] || fail "发送失败 state 期望 up 实得 $(state_field state)"
[ "$(state_field fail_streak)" = 1 ] || fail "发送失败应记 send_fail（fail_streak=1）实得 $(state_field fail_streak)"
grep -q "bridge send failed" "$STATE" || fail "send_fail 应带 bridge 发送失败原因: $(cat "$STATE")"
pass "bridge 不可达 → 回传 failed（refund 闭环）+ 记 send_fail 进 health"

# ── RF9 (F-RTS-001): 生成失败 → 必须回传 --send-result failed 退款 ──
# evaluate 已对 send 决策记账（energy/messages_without_reply+1/Hawkes）。生成失败分支
# （agent 整链返回失败，无文本产出）若只 record_health fail 不退款 → 未回复计数残留，
# 连续 5 轮 → silent 禁发（恢复后永久不发）。修复前红：生成失败分支不回传 → log 为空。
# 放在 case 7 之后：case 7 测 /send 发送失败（agent=success），此用例独立测生成失败（agent=fail）。
echo fail > "$FAKE_AGENT_MODE_FILE"    # spawn 失败；RPC 也是 fail → 整链生成失败
export SEND_RESULT_LOG="$TMP/sendresult_genfail.log"
rm -f "$SEND_RESULT_LOG"
: > "$SEND_RESULT_LOG"
# record_health fail 记账不回归（fail_streak 推进；fail_threshold=3 → 单次仍 up）
GFAIL_STREAK_BEFORE="$(state_field fail_streak)"
set +e; HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] && pass "生成失败 → tick 退出 1（既有语义保留）" || fail "生成失败应 exit 1, 实得 $RC"
# 退款回传必须发生：fake daemon --send-result 分支写入 SEND_RESULT_LOG（含 msg_id + failed）
grep -q 'abc123' "$SEND_RESULT_LOG" \
  && grep -q 'send-result' "$SEND_RESULT_LOG" \
  && grep -q 'failed' "$SEND_RESULT_LOG" \
  && pass "生成失败 → 回传 --send-result abc123 failed（退款发生，未回复计数回滚）" \
  || fail "生成失败应回传 --send-result <msg_id> failed, 实得: $(cat "$SEND_RESULT_LOG")"
# 生成失败退款必须区别于发送失败（error 带 generate_failed 标记）
grep -q 'generate_failed' "$SEND_RESULT_LOG" \
  && pass "生成失败 → --error generate_failed（区分生成段 vs 发送段）" \
  || fail "生成失败退款应带 generate_failed 标记: $(cat "$SEND_RESULT_LOG")"
[ "$(state_field fail_streak)" -gt "$GFAIL_STREAK_BEFORE" ] \
  && pass "生成失败 → record_health fail 推进 fail_streak（健康状态机不回归）" \
  || fail "生成失败应 record_health fail（fail_streak 未推进）"
echo success > "$FAKE_AGENT_MODE_FILE"   # 复位生成成功，供后续用例

# ── replan lockfile(5s 超时 + 陈旧锁 10min 接管,M15)──
# 直接调产品 schedule/replan._lock(R5):测试的是真实现,非逻辑拷贝
test_replan_lock() {
  local LOCK="$TMP/replan.lock"
  # 用例 1:占用中 → 5s 超时让位(_lock 返回 False)
  touch "$LOCK"
  local T0 T1
  T0="$(date +%s)"
  if "$REPO_ROOT/.venv/bin/python" -c "import sys; sys.path.insert(0, '$REPO_ROOT'); from schedule.replan import _lock; sys.exit(0 if _lock('$TMP') else 1)"; then
    fail "replan lockfile 占用中应超时让位"
  fi
  T1="$(date +%s)"
  [ $((T1 - T0)) -le 7 ] || fail "5s 超时未生效(耗时 $((T1 - T0))s)"
  # 用例 2:陈旧锁(mtime > 10min)→ 强制接管(_lock 返回 True)
  rm -f "$LOCK"
  touch -d "12 minutes ago" "$LOCK"
  "$REPO_ROOT/.venv/bin/python" -c "import sys; sys.path.insert(0, '$REPO_ROOT'); from schedule.replan import _lock; sys.exit(0 if _lock('$TMP') else 1)" || fail "陈旧锁应被接管"
  rm -f "$LOCK"
  pass "replan: lockfile 5s timeout and stale-lock takeover"
}
test_replan_lock

# ── R16: 并发锁移入专用 run 目录（不再用 /tmp/chiguo-tick.lock,防符号链接任意文件截断）──
test_tick_lock_dir() {
  local LOCK_DIR="$TMP/lockdir"
  CHIGUO_LOCK_DIR="$LOCK_DIR" HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1 || true
  # 运行时：锁文件落在 CHIGUO_LOCK_DIR 下（而非 /tmp）
  [ -f "$LOCK_DIR/chiguo-tick.lock" ] || fail "tick 锁应写在 CHIGUO_LOCK_DIR($LOCK_DIR) 下"
  # 源码层面：锁路径经 LOCK_FILE 变量 + exec 9>，不硬编码 /tmp
  grep -q 'LOCK_FILE="\$LOCK_DIR/chiguo-tick.lock"' "$REAL_TICK" \
    || fail "tick 锁未定义 LOCK_FILE=.../chiguo-tick.lock"
  grep -q 'exec 9>"\$LOCK_FILE"' "$REAL_TICK" || fail "tick 锁未经 exec 9>\"\$LOCK_FILE\""
  pass "R16: tick 锁移入 run 目录（$LOCK_DIR/chiguo-tick.lock）"
}
test_tick_lock_dir

# ── R18: replan-tick.sh 补 PATH export（cron 精简 PATH 下 node 可解析）──
test_replan_tick_path() {
  grep -q 'export PATH="\$PATH:/usr/local/bin:/opt/homebrew/bin"' "$REPO_ROOT/scripts/replan-tick.sh" \
    || fail "replan-tick.sh 缺 PATH 补齐 export（R18）"
  pass "R18: replan-tick.sh 含 PATH 补齐 export"
}
test_replan_tick_path

# ── #223 活动标记：ACTION=send 写活动文件（epoch 秒）；idle 判定不写 ──
test_activity_marker() {
  # 前面各用例均为 send 判定（多次运行）→ 活动文件应存在且新鲜
  [ -f "$CHIGUO_ACTIVITY_FILE" ] || fail "send 判定后应写活动文件"
  local ACT_TS NOW_TS
  ACT_TS="$(cat "$CHIGUO_ACTIVITY_FILE" 2>/dev/null || echo 0)"
  NOW_TS="$(date +%s)"
  [ $(( NOW_TS - ACT_TS )) -lt 300 ] || fail "活动时间戳应新鲜（差 $(( NOW_TS - ACT_TS ))s）"
  # idle 路径：ACTION != send → 不改写活动文件（exit 0）
  echo 1111111111 > "$CHIGUO_ACTIVITY_FILE"
  # R7 (F-RT-003): OWNER 前置检查要求登录态/或 toml 非占位收件人——用隔离 HOME（真实
  # credentials）跑 idle，否则占位/缺失收件人会在 --compact 之前 exit 1（与 send 路径同策略）
  FAKE_DAEMON_ACTION=idle HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1 \
    || fail "idle tick 应退出 0"
  [ "$(cat "$CHIGUO_ACTIVITY_FILE")" = 1111111111 ] || fail "idle 判定不应改写活动文件"
  # 源码层面：spawn 回退带 AGENTRUN_ROTATE_SESSION=1（send 每轮全新）
  grep -q 'AGENTRUN_ROTATE_SESSION=1' "$REAL_TICK" || fail "tick spawn 回退缺 AGENTRUN_ROTATE_SESSION=1"
  pass "#223 活动标记: send 写 / idle 不写 + spawn 轮换标志"
}
test_activity_marker

# ── R7 (F-RT-003): OWNER/node 缺失早退须前置到 --compact 之前，避免幻影记账 ──
# OWNER 缺失 → tick 应早于 evaluate 退出，chiguo_daemon.py --compact 根本不执行
# （决策记账不落盘）→ 无幻影。断言：① 行为——OWNER 缺失时 --compact 未被调用；
# ② 静态——tick.sh 源码中 OWNER 检查早于 --compact 调用。
test_owner_missing_no_phantom() {
  export COMPACT_LOG="$TMP/compact.log"
  : > "$COMPACT_LOG"
  # 清空收件人：无 credentials + 空 toml wechat_recipient → OWNER 缺失
  rm -f "$TMP/home/.chiguo/auth/wechat/credentials.json"
  cat > "$REPO/chiguo_proactive.toml" <<TOML
[host]
wechat_bridge_url = "http://127.0.0.1:$PORT/send"

[wechat]
wechat_recipient = ""
TOML
  set +e
  HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1
  RC=$?
  set -e
  [ "$RC" = 1 ] || fail "OWNER 缺失 tick 应 exit 1, 实得 $RC"
  [ ! -s "$COMPACT_LOG" ] && pass "OWNER 缺失 → --compact 未执行（无幻影决策记账）" \
    || fail "OWNER 缺失时不应调用 --compact，但 daemon 已被调用: $(cat "$COMPACT_LOG")"
  # 静态源码断言：OWNER 检查行号 < --compact 调用行号（前置）
  local owner_line compact_line
  owner_line="$(grep -n '未检测到收件人' "$REAL_TICK" | head -1 | cut -d: -f1)"
  compact_line="$(grep -n 'chiguo_daemon.py.*--compact' "$REAL_TICK" | head -1 | cut -d: -f1)"
  [ -n "$owner_line" ] && [ -n "$compact_line" ] \
    || fail "tick.sh 应含 OWNER 检查与 --compact (owner=$owner_line compact=$compact_line)"
  [ "$owner_line" -lt "$compact_line" ] \
    && pass "tick.sh OWNER 检查(L$owner_line)已前置到 --compact(L$compact_line) 之前" \
    || fail "OWNER 检查应早于 --compact 调用 (owner=$owner_line, compact=$compact_line)"
  # node 缺失检查同样须在 --compact 之前（node 缺失早退也避免幻影记账）
  local node_line
  node_line="$(grep -n 'command -v node' "$REAL_TICK" | head -1 | cut -d: -f1)"
  [ -n "$node_line" ] || fail "tick.sh 应含 node 缺失检查"
  [ "$node_line" -lt "$compact_line" ] \
    && pass "tick.sh node 缺失检查(L$node_line)已前置到 --compact(L$compact_line) 之前" \
    || fail "node 缺失检查应早于 --compact 调用 (node=$node_line, compact=$compact_line)"
  # 恢复后续用例（case 8 崩溃 tick）依赖的 toml：确保 send 路径配置完整
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
  printf '{"token":"t","userId":"real_openid@im.wechat","accountId":"a"}' \
    > "$TMP/home/.chiguo/auth/wechat/credentials.json"
}
test_owner_missing_no_phantom

# ── RF12 (M3): node 缺失 = 环境故障，告警 reason 与 agent 故障区分，不误诊 ──
# node 缺失（cron PATH 不完整/未安装）时 agent-run 无法执行 → 记 health fail（需 down/暂停），
# 但 reason 必须显式标注「环境问题，非 agent 故障」，告警文案据此区分，避免误诊为后端故障。
test_node_missing_env_fault() {
  # 重置 agent_health（避免沿用前面用例的 fail_reason 遮蔽 RF12 文案断言）
  rm -f "$REPO/agent_health.json"
  # 建一个不含 node 的迷你 PATH（其余工具软链系统路径），使 `command -v node` 失配。
  local MINIBIN="$TMP/minibin"
  mkdir -p "$MINIBIN"
  local t real_p
  for t in bash cat curl sed grep date dirname readlink mktemp mkdir sleep flock head wc printf cut tr realpath stat; do
    real_p="$(command -v "$t" 2>/dev/null || true)"
    [ -n "$real_p" ] && ln -sf "$real_p" "$MINIBIN/$t" 2>/dev/null || true
  done
  rm -f "$MINIBIN/node"   # 保证 node 缺席（PATH 查询不命中）
  # tick.sh 会追加 PATH 补齐（#85），须注入空补齐目录才能模拟 node 真缺失
  mkdir -p "$TMP/noboot"
  [ -x "$MINIBIN/bash" ] || fail "RF12: 迷你 PATH 缺 bash"
  local H2="$TMP/home_rf12"
  mkdir -p "$H2/.chiguo/auth/wechat"
  printf '{"token":"t","userId":"real_openid@im.wechat","accountId":"a"}' \
    > "$H2/.chiguo/auth/wechat/credentials.json"
  cat > "$REPO/chiguo_proactive.toml" <<TOML
[host]
wechat_bridge_url = "http://127.0.0.1:$PORT/send"

[wechat]
wechat_recipient = "owner@im.wechat"

[health]
fail_threshold = 3
TOML
  set +e
  HOME="$H2" CHIGUO_REPO="$REPO" CHIGUO_LOCK_DIR="$TMP/rf12_lock" \
    CHIGUO_PATH_BOOTSTRAP="$TMP/noboot" \
    PATH="$MINIBIN" bash "$REAL_TICK" >/dev/null 2>&1
  local RC=$?
  set -e
  [ "$RC" = 1 ] && pass "RF12: node 缺失 → tick exit 1" || fail "node 缺失应 exit 1, 实得 $RC"
  grep -q '环境问题，非 agent 故障' "$REPO/agent_health.json" \
    && pass "RF12: node 缺失 fail_reason 标注环境问题（区分 agent 故障）" \
    || fail "node 缺失 fail_reason 应含「环境问题，非 agent 故障」: $(cat "$REPO/agent_health.json")"
  grep -q '环境问题，非 agent 故障' "$REAL_TICK" \
    && pass "RF12: 静态——tick.sh node 缺失 reason 含环境标注" \
    || fail "tick.sh node 缺失 reason 应含「环境问题，非 agent 故障」"
  grep -q '连续.*次失败（原因' "$REPO/scripts/agent_health.py" \
    && pass "RF12: agent_health 告警文案不再硬说 pi-agent，中性化（原因区分归属）" \
    || fail "agent_health 告警文案应中性化（连续 N 次失败 + reason）"
  # 恢复 send 路径 toml 供后续用例
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
}
test_node_missing_env_fault

# ── 用例 8（Issue #135）: daemon --compact 崩溃 → 非零退出 + stderr 告警（不得静默吞掉）──
# 放在最末尾：替换 fake daemon 为崩溃版，不影响前面各用例依赖的 send 输出
cat > "$REPO/chiguo_daemon.py" <<'PY'
import sys
if "--compact" in sys.argv:
    print("崩溃啦", file=sys.stderr)
    sys.exit(3)
sys.exit(0)
PY
CRASH_LOG="$TMP/tick_crash.log"
set +e
CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>"$CRASH_LOG"
RC=$?
set -e
[ "$RC" != 0 ] || fail "daemon --compact 崩溃时 tick 应非零退出（当前被 || true 吞掉）, 实得 $RC"
grep -q "chiguo-tick" "$CRASH_LOG" || fail "daemon 崩溃应输出 [chiguo-tick] 告警到 stderr: $(cat "$CRASH_LOG")"
pass "daemon --compact 崩溃 → tick 非零退出 + stderr 告警（不再静默）"

echo "test_tick_health: 全部通过"
