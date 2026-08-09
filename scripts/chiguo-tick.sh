#!/usr/bin/env bash
# chiguo-tick — 系统 crontab 入口
set -euo pipefail
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
PY="$REPO/.venv/bin/python"
# memory-lancedb-pro 扩展的 smart extraction 需要 key（cron 环境无该变量）;来源单一 = scripts/pi-auth.sh
source "$(dirname "$(readlink -f "$0")")/pi-auth.sh"
OUT="$("$PY" "$REPO/chiguo_daemon.py" --compact 2>/dev/null || true)"
ACTION="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("action",""))
except: print("")' 2>/dev/null || true)"
[ "$ACTION" = "send" ] || exit 0
# 发送目标/端点（提前解析：失败分支记账告警也要用）
# 收件人解析链：登录后的 ~/.chiguo/auth/wechat/credentials.json userId（真实）→ toml wechat_recipient（用户手配）→ 失败提示 login
OWNER="$(sed -n 's/.*"userId"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$HOME/.chiguo/auth/wechat/credentials.json" 2>/dev/null | head -1 || true)"
if [ -z "$OWNER" ] || [ "$OWNER" = "owner@im.wechat" ]; then
  OWNER="$(grep -oP '(?<=wechat_recipient = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
fi
if [ -z "$OWNER" ] || [ "$OWNER" = "owner@im.wechat" ]; then
  echo "[chiguo-tick] 未检测到收件人（登录后自动注入：bash scripts/wechat-bridge.sh login；或 toml 配 wechat_recipient）" >&2
  exit 1
fi
BRIDGE_URL="$(grep -oP '(?<=wechat_bridge_url = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$BRIDGE_URL" ] || BRIDGE_URL="http://127.0.0.1:18790/send"
# #84 共享 token:bridge 配置了 WECHAT_BRIDGE_TOKEN 时同源传入,未配置时零 header(向后兼容)
TOKEN_HDR=()
[ -n "${WECHAT_BRIDGE_TOKEN:-}" ] && TOKEN_HDR=(-H "X-Bridge-Token: $WECHAT_BRIDGE_TOKEN")
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
      -H 'Content-Type: application/json' "${TOKEN_HDR[@]}" -d "$body" >/dev/null 2>&1 \
      || echo "[chiguo-tick] 告警/恢复发送失败（transition=$trans），下个 tick 不再重发" >&2
  fi
}
# ── A8: 生成失败确定性回退 ──
# pi 失败 → composer 模板池兜底（零 LLM）：decision JSON 落盘传给
# chiguo_composer.py CLI，成功则照常发送（health 记 success + record-send 打 fallback 标记）；
# composer 也失败才 record_health fail + exit 1。
if [ -z "$TEXT" ]; then
  echo "[chiguo-tick] pi-run 未生成消息: $(printf '%s' "$RES" | head -c 300)" >&2
  DECISION_FILE="$(mktemp "${TMPDIR:-/tmp}/chiguo-fallback-XXXXXX.json")"
  printf '%s' "$OUT" > "$DECISION_FILE"
  FALLBACK_TEXT="$("$PY" "$REPO/chiguo_composer.py" "$DECISION_FILE" 2>/dev/null || true)"
  rm -f "$DECISION_FILE"
  if [ -n "$FALLBACK_TEXT" ]; then
    TEXT="$FALLBACK_TEXT"
    COMPOSER_FALLBACK=1
    echo "[chiguo-tick] pi 失败,composer 兜底生成消息" >&2
  fi
fi
if [ -z "$TEXT" ]; then
  FAIL_REASON="$(printf '%s' "$RES" | python3 -c 'import json,sys
try: print((json.load(sys.stdin).get("error") or "")[:100])
except: print("")' 2>/dev/null || true)"
  [ -n "$FAIL_REASON" ] || FAIL_REASON="tick pi-run 未生成消息"
  record_health fail "$FAIL_REASON"
  exit 1
fi
# 消息已产出（pi 或 composer 兜底）→ 先记 success（发送失败不丢成功信号）；再发送
record_health success
BODY="$(python3 -c 'import json,sys; print(json.dumps({"to": sys.argv[1], "text": sys.argv[2]}))' "$OWNER" "$TEXT")"
MSG_ID="$(printf '%s' "$OUT" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("msg_id",""))
except: print("")' 2>/dev/null || true)"
if ! curl -sf --noproxy '*' -X POST "$BRIDGE_URL" \
  -H 'Content-Type: application/json' "${TOKEN_HDR[@]}" -d "$BODY" >/dev/null 2>&1; then
  echo "[chiguo-tick] bridge 发送失败，下个 tick 重试" >&2
  # 回传失败 → daemon 侧 refund_send（energy/quota 回滚 + Hawkes 事件剔除），反馈闭环不静默断
  if [ -n "$MSG_ID" ]; then
    "$PY" "$REPO/chiguo_daemon.py" --send-result "$MSG_ID" --send-status failed --error "bridge unreachable" >/dev/null 2>&1 \
      || echo "[chiguo-tick] send-result(failed) 回传失败 msg_id=$MSG_ID" >&2
  fi
  exit 0
fi
# 回传发送结果（A8: composer 兜底时额外打 fallback 标记，health 已记 success）
if [ -n "$MSG_ID" ]; then
  if [ -n "${COMPOSER_FALLBACK:-}" ]; then
    "$PY" "$REPO/chiguo_daemon.py" --record-send "$MSG_ID" --text "$TEXT" --fallback >/dev/null 2>&1 \
      || echo "[chiguo-tick] record-send 失败 msg_id=$MSG_ID" >&2
  else
    "$PY" "$REPO/chiguo_daemon.py" --record-send "$MSG_ID" --text "$TEXT" >/dev/null 2>&1 \
      || echo "[chiguo-tick] record-send 失败 msg_id=$MSG_ID" >&2
  fi
fi
