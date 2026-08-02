#!/usr/bin/env bash
# chiguo-tick — 系统 crontab 入口（替代 openclaw cron trigger-script）
set -euo pipefail
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
PY="$REPO/.venv/bin/python"
# memory-lancedb-pro 扩展的 smart extraction 需要 key（cron 环境无该变量）
# 来源单一：~/.pi/agent/auth.json——优先 opencode-go 条目（扩展 json5 llm 端点固定 opencode 网关），
# 无则回退 [host].provider 对应条目（install_pi.sh 写入；缺省 opencode-go）
if [ -z "${OPENCODE_API_KEY:-}" ] && [ -f "$HOME/.pi/agent/auth.json" ]; then
  TICK_FALLBACK_PROVIDER="$(sed -n 's/^[[:space:]]*provider *= *"\([^"]*\)".*/\1/p' "$REPO/chiguo_proactive.toml" | head -1 || true)"
  [ -n "$TICK_FALLBACK_PROVIDER" ] || TICK_FALLBACK_PROVIDER=opencode-go
  OPENCODE_API_KEY="$(TICK_FALLBACK_PROVIDER="$TICK_FALLBACK_PROVIDER" python3 -c "
import json,os
try:
    d=json.load(open(os.path.expanduser('~/.pi/agent/auth.json')))
    key = (d.get('opencode-go') or {}).get('key') or (d.get(os.environ.get('TICK_FALLBACK_PROVIDER','opencode-go')) or {}).get('key','')
    print(key or '')
except Exception: print('')
" 2>/dev/null || true)"
  export OPENCODE_API_KEY
fi
OUT="$("$PY" "$REPO/chiguo_daemon.py" --compact 2>/dev/null || true)"
ACTION="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("action",""))
except: print("")' 2>/dev/null || true)"
[ "$ACTION" = "send" ] || exit 0
# 发送目标/端点（提前解析：失败分支记账告警也要用）
OWNER="$(grep -oP '(?<=wechat_recipient = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
if [ -z "$OWNER" ]; then
  echo "[chiguo-tick] toml 缺 wechat_recipient" >&2
  exit 1
fi
BRIDGE_URL="$(grep -oP '(?<=wechat_bridge_url = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$BRIDGE_URL" ] || BRIDGE_URL="http://127.0.0.1:18790/send"
# 生成消息（主动发送用独立会话 chiguo-send，与回复侧 chiguo-main 分离 → 跨进程零并发 turn；
# 决策 JSON 自足，无需对话连续性；值从 toml [host].send_session_id 读，缺省回退 chiguo-send）
SEND_SESSION="$(grep -oP '(?<=send_session_id = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$SEND_SESSION" ] || SEND_SESSION="chiguo-send"
RES="$(CHIGUO_REPO="$REPO" PIRUN_SESSION="$SEND_SESSION" node "$REPO/scripts/pi-run.mjs" --prompt "$OUT" --send-mode 2>/dev/null || true)"
TEXT="$(printf '%s' "$RES" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("text",""))
except: print("")' 2>/dev/null || true)"
# pi 假死记账：成败记入 pi_health 状态机，transition 时经 /send 发告警/恢复（零额外 pi 调用）
# 注：toml 缺 wechat_recipient 时（上方提前 exit）pi 未跑 → 该次不记账
record_health() {
  local out trans msg body
  out="$("$PY" "$REPO/scripts/pi_health.py" record --outcome "$1" --reason "${2:-}" 2>/dev/null || true)"
  IFS='|' read -r trans msg <<< "$(printf '%s' "$out" | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("transition","") + "|" + d.get("message",""))
except: print("|")' 2>/dev/null || true)"
  if [ -n "$trans" ] && [ "$trans" != "none" ] && [ -n "$msg" ]; then
    body="$(python3 -c 'import json,sys; print(json.dumps({"to": sys.argv[1], "text": sys.argv[2]}))' "$OWNER" "$msg")"
    curl -sf --noproxy '*' -X POST "$BRIDGE_URL" \
      -H 'Content-Type: application/json' -d "$body" >/dev/null 2>&1 \
      || echo "[chiguo-tick] 告警/恢复发送失败（transition=$trans），下个 tick 不再重发" >&2
  fi
}
if [ -z "$TEXT" ]; then
  echo "[chiguo-tick] pi-run 未生成消息: $(printf '%s' "$RES" | head -c 300)" >&2
  FAIL_REASON="$(printf '%s' "$RES" | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("error") or "")[:100])
except: print("")' 2>/dev/null || true)"
  [ -n "$FAIL_REASON" ] || FAIL_REASON="tick pi-run 未生成消息"
  record_health fail "$FAIL_REASON"
  exit 1
fi
# pi 已产出消息 → 先记 success（发送失败不丢成功信号）；再发送
record_health success
BODY="$(python3 -c 'import json,sys; print(json.dumps({"to": sys.argv[1], "text": sys.argv[2]}))' "$OWNER" "$TEXT")"
if ! curl -sf --noproxy '*' -X POST "$BRIDGE_URL" \
  -H 'Content-Type: application/json' -d "$BODY" >/dev/null 2>&1; then
  echo "[chiguo-tick] bridge 发送失败，下个 tick 重试" >&2
  exit 0
fi
# 回传发送结果
MSG_ID="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("msg_id",""))
except: print("")' 2>/dev/null || true)"
if [ -n "$MSG_ID" ]; then
  "$PY" "$REPO/chiguo_daemon.py" --record-send "$MSG_ID" --text "$TEXT" >/dev/null 2>&1 \
    || echo "[chiguo-tick] record-send 失败 msg_id=$MSG_ID" >&2
fi
