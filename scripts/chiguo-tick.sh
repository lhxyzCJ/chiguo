#!/usr/bin/env bash
# chiguo-tick — 系统 crontab 入口（替代 openclaw cron trigger-script）
set -euo pipefail
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
PY="$REPO/.venv/bin/python"
OUT="$("$PY" "$REPO/chiguo_daemon.py" --compact 2>/dev/null || true)"
ACTION="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("action",""))
except: print("")' 2>/dev/null || true)"
[ "$ACTION" = "send" ] || exit 0
# 生成消息
RES="$(node "$REPO/scripts/pi-run.mjs" --prompt "$OUT" 2>/dev/null || true)"
TEXT="$(printf '%s' "$RES" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("text",""))
except: print("")' 2>/dev/null || true)"
if [ -z "$TEXT" ]; then
  echo "[chiguo-tick] pi-run 未生成消息: $(printf '%s' "$RES" | head -c 300)" >&2
  exit 1
fi
# 发送（owner 从 toml 读）
OWNER="$(grep -oP '(?<=wechat_recipient = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
if [ -z "$OWNER" ]; then
  echo "[chiguo-tick] toml 缺 wechat_recipient" >&2
  exit 1
fi
BODY="$(python3 -c 'import json,sys; print(json.dumps({"to": sys.argv[1], "text": sys.argv[2]}))' "$OWNER" "$TEXT")"
if ! curl -sf --noproxy '*' -X POST http://127.0.0.1:18790/send \
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
