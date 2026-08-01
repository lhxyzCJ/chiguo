#!/usr/bin/env bash
# chiguo-tick — 系统 crontab 入口（替代 openclaw cron trigger-script）
set -euo pipefail
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
PY="$REPO/.venv/bin/python"
# memory-lancedb-pro 扩展的 smart extraction 需要 opencode-go key（cron 环境无该变量）
# 来源单一：~/.pi/agent/auth.json 的 opencode-go 条目（install_pi.sh 阶段 5 写入）
if [ -z "${OPENCODE_API_KEY:-}" ] && [ -f "$HOME/.pi/agent/auth.json" ]; then
  OPENCODE_API_KEY="$(python3 -c "
import json,os
try:
    d=json.load(open(os.path.expanduser('~/.pi/agent/auth.json')))
    print(d.get('opencode-go',{}).get('key',''))
except Exception: print('')
" 2>/dev/null || true)"
  export OPENCODE_API_KEY
fi
OUT="$("$PY" "$REPO/chiguo_daemon.py" --compact 2>/dev/null || true)"
ACTION="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("action",""))
except: print("")' 2>/dev/null || true)"
[ "$ACTION" = "send" ] || exit 0
# 生成消息（主动发送用独立会话 chiguo-send，与回复侧 chiguo-main 分离 → 跨进程零并发 turn；
# 决策 JSON 自足，无需对话连续性；值从 toml [host].send_session_id 读，缺省回退 chiguo-send）
SEND_SESSION="$(grep -oP '(?<=send_session_id = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$SEND_SESSION" ] || SEND_SESSION="chiguo-send"
RES="$(CHIGUO_REPO="$REPO" PIRUN_SESSION="$SEND_SESSION" node "$REPO/scripts/pi-run.mjs" --prompt "$OUT" --send-mode 2>/dev/null || true)"
TEXT="$(printf '%s' "$RES" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("text",""))
except: print("")' 2>/dev/null || true)"
if [ -z "$TEXT" ]; then
  echo "[chiguo-tick] pi-run 未生成消息: $(printf '%s' "$RES" | head -c 300)" >&2
  exit 1
fi
# 发送（owner/端点从 toml 读；[host].wechat_bridge_url 缺省回退默认端点）
OWNER="$(grep -oP '(?<=wechat_recipient = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
if [ -z "$OWNER" ]; then
  echo "[chiguo-tick] toml 缺 wechat_recipient" >&2
  exit 1
fi
BRIDGE_URL="$(grep -oP '(?<=wechat_bridge_url = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$BRIDGE_URL" ] || BRIDGE_URL="http://127.0.0.1:18790/send"
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
