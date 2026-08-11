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
http.createServer((req, res) => {
  let b = ''
  req.on('data', (c) => { b += c })
  req.on('end', () => {
    fs.appendFileSync(process.argv[2], JSON.stringify({ url: req.url, body: b }) + '\n')
    if (req.url === '/agent/prompt') {
      let rpc = false
      try { rpc = fs.readFileSync(rpcModeFile, 'utf8').trim() === 'success' } catch {}
      res.end(rpc ? JSON.stringify({ ok: true, text: 'RPC 主动消息' }) : JSON.stringify({ ok: false, error: 'mock RPC 故障' }))
    } else {
      res.end('{"ok":true}')
    }
  })
}).listen(Number(process.argv[3]), '127.0.0.1')
JS
node "$TMP/recorder.js" "$POST_LOG" "$PORT" "$TMP/rpc_mode" &
SRV_PID=$!
RPC_MODE="$TMP/rpc_mode"
echo fail > "$RPC_MODE"

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

# ── 用例 4.5: A8 composer 兜底——agent 失败但 composer 模板池兜底成功 → 照常发送 + success ──
# fake repo 放入真实 chiguo_composer.py（无 personality 目录 → 无台词模板 → intent 文本兜底，
# 仍产出非空文本）→ tick 应退出 0、发出消息、health 记 success（fail_streak 归零）。
cp "$REPO_ROOT/chiguo_composer.py" "$REPO/chiguo_composer.py"
echo fail > "$FAKE_AGENT_MODE_FILE"
POST_BEFORE="$(post_count)"
set +e; CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "composer 兜底成功时 tick 应退出 0, 实得 $RC"
[ "$(post_count)" = $((POST_BEFORE + 1)) ] \
  && pass "composer 兜底: 照常发出 1 条消息" || fail "期望 $((POST_BEFORE + 1)) POST 实得 $(post_count)"
[ "$(state_field state)" = up ] || fail "composer 兜底后 health 应仍为 up, 实得 $(state_field state)"
[ "$(state_field fail_streak)" = 0 ] || fail "composer 兜底应记 success（fail_streak 归零）, 实得 $(state_field fail_streak)"
rm -f "$REPO/chiguo_composer.py"

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

# ── 用例 7: bridge 不可达 → 回传 --send-result failed（refund 反馈闭环不断）──
kill ${SRV_PID:-} 2>/dev/null || true
export SEND_RESULT_LOG="$TMP/sendresult.log"
: > "$SEND_RESULT_LOG"
set +e; HOME="$TMP/home" CHIGUO_REPO="$REPO" bash "$REAL_TICK" >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] || fail "bridge 不可达 tick 应退出 1（发送失败，issue #85）"
grep -q "send-result.*failed" "$SEND_RESULT_LOG" \
  || fail "bridge 不可达应回传 --send-result failed: $(cat "$SEND_RESULT_LOG")"
pass "bridge 不可达 → 回传 failed（refund 闭环）"

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
