#!/usr/bin/env bash
# chiguo-tick — 系统 crontab 入口
set -euo pipefail
# cron 环境 PATH 精简，补齐 node/python3 常用安装目录
# （issue #85: crontab 缺 /usr/local/bin、/opt/homebrew/bin 时裸 node/python3 找不到）
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
# 并发锁：上一 tick 未结束时跳过（cron 重入防护，拿不到锁即退出 0）
# R16: 锁移入 $HOME/.chiguo/run（/tmp 世界可写 → 符号链接指向任意文件被 O_TRUNC 截断攻击面）
LOCK_DIR="${CHIGUO_LOCK_DIR:-$HOME/.chiguo/run}"
LOCK_FILE="$LOCK_DIR/chiguo-tick.lock"
mkdir -p "$LOCK_DIR" || { echo "[chiguo-tick] 锁目录不可写: $LOCK_DIR" >&2; exit 1; }
exec 9>"$LOCK_FILE"  # flock 拿不到（文件打开失败）由 set -e 兜住，脚本非零退出
if ! flock -n 9; then
    echo "[chiguo-tick] 已有实例运行，跳过本 tick" >&2
    exit 0
fi
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
PY="$REPO/.venv/bin/python"
# pi 生成需要 LLM key（cron 环境无该变量）;来源单一 = scripts/agent-auth.sh
source "$(dirname "$(readlink -f "$0")")/agent-auth.sh"
# R7 (F-RT-003): OWNER（收件人）缺失 / node 缺失等前置检查必须发生在 --compact（决策
# 记账）之前——否则 evaluate 已对 send 决策记账（energy/messages/Hawkes/逃生阀冷却），
# 随后早退却无 --send-result 回传 → 幻影记账。OWNER 来自登录态/toml、node 来自 PATH，
# 均不依赖 evaluate 输出，故前置到 --compact 之前是正解（evaluate 根本不执行 → 零记账）。
# ── 发送目标/端点（提前解析：失败分支记账告警也要用）──
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
# cron 环境无 .env → 从 wechat-bridge/.env 读（wechat-bridge.sh write_env 生成随机 token）
TOKEN_HDR=()
[ -n "${WECHAT_BRIDGE_TOKEN:-}" ] || WECHAT_BRIDGE_TOKEN="$(grep -oP '(?<=^WECHAT_BRIDGE_TOKEN=).*' "$REPO/wechat-bridge/.env" 2>/dev/null | head -1 || true)"
[ -n "${WECHAT_BRIDGE_TOKEN:-}" ] && TOKEN_HDR=(-H "X-Bridge-Token: $WECHAT_BRIDGE_TOKEN")
# 生成消息（主动发送用独立会话 chiguo-send，与回复侧 chiguo-main 分离 → 跨进程零并发 turn；
# 决策 JSON 自足，无需对话连续性；值从 toml [host].send_session_id 读，缺省回退 chiguo-send）
SEND_SESSION="$(grep -oP '(?<=send_session_id = ")[^"]+' "$REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$SEND_SESSION" ] || SEND_SESSION="chiguo-send"
# agent 假死记账：成败记入 agent_health 状态机，transition 时经 /send 发告警/恢复（零额外 agent 调用）
# 注：toml 缺 wechat_recipient 时（上方提前 exit）agent 未跑 → 该次不记账
record_health() {
  local out trans msg body recstate
  out="$("$PY" "$REPO/scripts/agent_health.py" record --outcome "$1" --reason "${2:-}" 2>/dev/null || true)"
  IFS='|' read -r trans msg state <<< "$(printf '%s' "$out" | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("transition","") + "|" + d.get("message","") + "|" + d.get("state",""))
except: print("||")' 2>/dev/null || true)"
  REC_STATE="${state:-}"
  REC_TRANS="${trans:-}"
  if [ -n "$trans" ] && [ "$trans" != "none" ] && [ -n "$msg" ]; then
    body="$("$PY" -c 'import json,sys; print(json.dumps({"to": sys.argv[1], "text": sys.argv[2]}))' "$OWNER" "$msg")"
    curl -sf --max-time 10 --connect-timeout 5 --noproxy '*' -X POST "$BRIDGE_URL" \
      -H 'Content-Type: application/json' "${TOKEN_HDR[@]}" -d "$body" >/dev/null 2>&1 \
      || echo "[chiguo-tick] 告警/恢复发送失败（transition=$trans），下个 tick 不再重发" >&2
  fi
}
# node 缺失（cron PATH 不完整时兜底）：显式记账 fail + 告警，而非静默降级。
# RF12 (M3): reason 显式标注「环境问题，非 agent 故障」——node 缺失是 cron PATH/安装
# 环境问题，不是 agent（后台）故障。仍归入 health fail_streak（agent-run 无法执行 →
# 需要 down/暂停 + 告警），但告警文案经 reason 区分环境 vs agent，避免误诊为后端异常。
if ! command -v node >/dev/null 2>&1; then
  echo "[chiguo-tick] node 缺失（环境问题：cron PATH 不完整或未安装 node），agent-run 无法执行，记录 health fail" >&2
  record_health fail "tick node 缺失（环境问题，非 agent 故障）"
  exit 1
fi
# ── 以下进入 evaluate（决策记账）阶段：前置检查已通过，才执行 --compact。──
# Issue #135: daemon --compact 失败不能被静默吞掉——保留退出码，非零时告警到 stderr（idle 静默 exit 0 语义不变）
# 一次执行同时捕获 stdout/stderr/退出码（stderr 走临时文件，避免与 OUT 混流）
DAEMON_ERR_FILE="$(mktemp "${TMPDIR:-/tmp}/chiguo-tick-daemon-XXXXXX.err")"
OUT="$("$PY" "$REPO/chiguo_daemon.py" --compact 2>"$DAEMON_ERR_FILE")" || {
  rc=$?
  echo "[chiguo-tick] daemon --compact 失败（exit $rc）：$(head -c 300 "$DAEMON_ERR_FILE")" >&2
  rm -f "$DAEMON_ERR_FILE"
  exit 1
}
rm -f "$DAEMON_ERR_FILE"
ACTION="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("action",""))
except: print("")' 2>/dev/null || true)"
[ "$ACTION" = "send" ] || exit 0
# 活动标记（#223 轮换空闲保护）：cron 判定要发消息 = 会话活动 → 主会话每日轮换顺延。
# 与 bridge onMessage 的用户消息活动共用 ~/.chiguo/session-activity-last（epoch 秒）。
ACTIVITY_FILE="${CHIGUO_ACTIVITY_FILE:-$HOME/.chiguo/session-activity-last}"
mkdir -p "$(dirname "$ACTIVITY_FILE")" 2>/dev/null || true
date +%s > "$ACTIVITY_FILE" 2>/dev/null || true
# ── v1.11 B1: 发送侧 RPC 优先（经 bridge /agent/prompt 转发常驻 AgentRpc），失败回退 spawn ──
# 决策 JSON 自足（chiguo-send 会话）→ RPC 生成消息；RPC 不可用/超时/空回复 → 回退
# node agent-run.mjs --send-mode。U2 (#227): 无 composer 兜底——生成失败 sleep
# retry_delay_seconds(默认5) 整链重试一次，仍失败 → record_health fail + exit 1；
# 状态为 down（暂停）时失败路径 exit 0 静默跳过不发。
#
# R10 (F-A17-004) 三层超时对齐原则：
#   - 本 curl 对 /agent/prompt 的 `--max-time 125` 是本链**外层时钟**；
#   - bridge 侧（bridge.mjs handleAgentPrompt）对 send 侧已对齐总预算
#     SEND_PROMPT_TOTAL_MS(默认 110s) —— 排队(queue) + restart(≤3s) + ensureStarted
#     + prompt(agent-rpc 120s) 全部计入 budget，且排队超 SEND_PROMPT_QUEUE_WAIT_MS
#     (默认 30s) → 快速判败 `queue_busy`（被取消的 turn 不执行，不留孤儿 LLM）。
#   - 因此 RPC 侧**要么在 110s ≤ 125s 内给确定结果，要么快速判败**——本 curl 不会先超时
#     触发 `--max-time 125`，从而不出现"RPC 仍在执行却已回退 spawn"的双 LLM 并行窗口。
#   - 改 bridge 侧预算/本 curl 超时时须保持  bridgeTotal ≤ 125s（留 curl 网络余量）。
#   - 回退封顶：单 tick 内 RPC + spawn 的**生成尝试**合计 ≤ MAX_GEN_ATTEMPTS（下方有界循环），
#     对齐 R7 的 fail_streak 有界语义，防止极端故障下整链无限重试拖长单 tick。
KEEP_TICK_RETRY_S="$(grep -oP '(?<=retry_delay_seconds = )\d+' "$REPO/chiguo_proactive.toml" | head -1 || echo 5)"
# 生成尝试封顶（RPC 一次 + spawn 一次 视为一轮「整链一次」）。当前=重试一次，共 2 轮。
# 每轮内 RPC 失败 1 次 + spawn 失败 1 次；合计生成尝试 ≤ 2×2=4，后续全部由 record_health 走 fail。
MAX_GEN_ATTEMPTS=2
KEEP_TICK_HEALTH="$REPO/agent_health.json"
# 读 agent_health 当前态：down → 本次失败路径 exit 0（暂停；cron 15min 天然节奏，恢复靠 probe 成功）
DOWN_BEFORE=0
if [ -f "$KEEP_TICK_HEALTH" ]; then
  if python3 -c 'import json,sys
try:
    d = json.load(open(sys.argv[1]))
    sys.exit(0 if d.get("state") == "down" else 1)
except Exception:
    sys.exit(1)' "$KEEP_TICK_HEALTH" 2>/dev/null; then
    DOWN_BEFORE=1
  fi
fi

# 完整生成链一次（RPC 优先 → spawn 回退），结果写全局 RES/TEXT
try_generate() {
  RES=""
  if [ -n "$BRIDGE_URL" ]; then
    RPC_URL="${BRIDGE_URL%%/send*}/agent/prompt"
    PROMPT_FILE="$(mktemp "${TMPDIR:-/tmp}/chiguo-rpc-prompt-XXXXXX.json")"
    printf '%s' "$OUT" > "$PROMPT_FILE"
    RPC_BODY="$("$PY" -c 'import json,sys; print(json.dumps({"text": open(sys.argv[1]).read(), "mode": "send"}))' "$PROMPT_FILE" 2>/dev/null || true)"
    rm -f "$PROMPT_FILE"
    if [ -n "$RPC_BODY" ]; then
      RPC_RES="$(curl --max-time 125 --connect-timeout 5 --noproxy '*' -s -X POST "$RPC_URL" \
        -H 'Content-Type: application/json' "${TOKEN_HDR[@]}" -d "$RPC_BODY" 2>/dev/null || true)"
      RES="$(printf '%s' "$RPC_RES" | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(json.dumps({"ok": True, "text": d["text"]}) if d.get("ok") and d.get("text") else "")
except: print("")' 2>/dev/null || true)"
    fi
  fi
  if [ -z "$RES" ]; then
    # R10 分流：queue_busy = bridge 在预算内快速判败（排队超预算），是确定失败 → spawn 立即接管；
    # 其他 RPC 失败/超时/空回复同样回退 spawn。两者都不留双 LLM：bridge 已保证不返回后才继续跑。
    RPC_ERR="$(printf '%s' "${RPC_RES:-}" | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(str(d.get("error") or "")[:160])
except: print("")' 2>/dev/null || true)"
    [ -n "$RPC_ERR" ] || RPC_ERR="${RPC_RES:-（无 RPC 响应）}"
    echo "[chiguo-tick] RPC 未产出,回退 spawn agent-run: $RPC_ERR" >&2
    RES="$(CHIGUO_REPO="$REPO" AGENTRUN_SESSION="$SEND_SESSION" AGENTRUN_ROTATE_SESSION=1 node "$REPO/scripts/agent-run.mjs" --prompt "$OUT" --send-mode 2>/dev/null || true)"
  fi
  TEXT="$(printf '%s' "$RES" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("text",""))
except: print("")' 2>/dev/null || true)"
}

# ── 生成链有界重试（R10 封顶）：最多 MAX_GEN_ATTEMPTS 轮整链（每轮 RPC→spawn 回退）。
# 核心语义与原先一致：首轮直接跑；空则 sleep 抖动缓冲后重试一次；仍空 → 上报 fail 退出。
# 差别是把"恰两次"写成显式有界循环 + 尝试计数，杜绝未来加码导致的重试失控/单 tick 拖长。
GEN_ATTEMPT=0
while [ "$GEN_ATTEMPT" -lt "$MAX_GEN_ATTEMPTS" ]; do
  GEN_ATTEMPT=$((GEN_ATTEMPT + 1))
  # 抖动缓冲：非首轮才 sleep retry_delay_seconds(默认5)（重试成功不计 fail_streak）
  if [ "$GEN_ATTEMPT" -gt 1 ] && [ -n "$KEEP_TICK_RETRY_S" ] && [ "$KEEP_TICK_RETRY_S" -gt 0 ] 2>/dev/null; then
    sleep "$KEEP_TICK_RETRY_S"
  fi
  try_generate
  [ -n "$TEXT" ] && break
done
if [ -z "$TEXT" ]; then
  echo "[chiguo-tick] agent-run 未生成消息: $(printf '%s' "$RES" | head -c 300)" >&2
  FAIL_REASON="$(printf '%s' "$RES" | "$PY" -c 'import json,sys
try: print((json.load(sys.stdin).get("error") or "")[:100])
except: print("")' 2>/dev/null || true)"
  [ -n "$FAIL_REASON" ] || FAIL_REASON="tick agent-run 未生成消息"
  # RF9 (F-RTS-001): 生成失败必须退款——evaluate 已对该 send 决策记账（energy/quota/
  # messages_without_reply+1/Hawkes/逃生阀冷却），生成失败只 record_health fail 而不退款
  # 会让未回复计数残留，连续失败 → backoff_level==2(silent) → evaluate_triggers 直接
  # return None → 恢复后永久不发普通消息。故除 record_health fail 外，还要从决策 JSON
  # 取 msg_id 回传 --send-result failed 走退款闭环（refund_send 回滚未回复/额度/Hawkes）。
  # 对齐发送失败分支（下方 MSG_ID 解析 + --send-result failed 回传）的调用形态。
  GEN_MSG_ID="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("msg_id",""))
except: print("")' 2>/dev/null || true)"
  if [ -n "$GEN_MSG_ID" ]; then
    "$PY" "$REPO/chiguo_daemon.py" --send-result "$GEN_MSG_ID" --send-status failed --error "generate_failed: $FAIL_REASON" >/dev/null 2>&1 \
      || echo "[chiguo-tick] send-result(failed) 回传失败 msg_id=$GEN_MSG_ID" >&2
  fi
  record_health fail "$FAIL_REASON"
  # 本 tick 开始时已 down（暂停态）→ exit 0 静默跳过（cron 15min 天然节奏，
  # 恢复靠 probe 成功：agent 修好后下个 cron 生成成功 → REC_STATE=up + 恢复消息）；
  # 触发 down 的那次仍 exit 1（与既有语义一致，未达阈值前逐次失败同样 exit 1）
  if [ "$DOWN_BEFORE" = 1 ]; then
    exit 0
  fi
  exit 1
fi
# 消息已产出 → 不再这里记 success（F-A6-2：发送失败也进健康状态机，成功必须是
# 生成+发送都 OK；success 移到发送成功分支，避免发送前清零导致 send_fail 永不累积）
BODY="$("$PY" -c 'import json,sys; print(json.dumps({"to": sys.argv[1], "text": sys.argv[2]}))' "$OWNER" "$TEXT")"
MSG_ID="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("msg_id",""))
except: print("")' 2>/dev/null || true)"
# A2: 把决策 JSON 的 trigger 传给 --record-send —— 否则 cron 主链路 record_send_text
# 的 `if trigger:` 恒不成立，reply_stats 只有 replied 没有 sent，反馈闭环永不激活。
TRIGGER="$(printf '%s' "$OUT" | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin).get("trigger",""))
except: print("")' 2>/dev/null || true)"
# 发送并透传失败原因（#224: "prepare failed" = context_token 过期 → 显式恢复指引，
# 而不是笼统的"发送失败"——从微信给机器人发一条消息即刷新，无需重新扫码）
# 主消息发送超时取 35s，与 chiguo_daemon.py _loop_send 的 /send 超时（35s）保持一致
# （#261/CR-2: 对齐双路径超时，避免 cron 11-35s 窗口行为分叉；改此值须同步改 daemon）
CURL_RC=0
SEND_RESP="$(curl -s --max-time 35 --connect-timeout 5 --noproxy '*' -X POST "$BRIDGE_URL" \
  -H 'Content-Type: application/json' "${TOKEN_HDR[@]}" -d "$BODY" 2>&1 || CURL_RC=$?)"
# RF1 (M6-2): 发送成功判定升级为 JSON 解析（对齐 loop.py 的 API 解析语义），不再用
# 子串 grep '"ok": *true'。语义分档：
#   ok       —— 合法 JSON 且 ok===true → 确定成功；
#   not_ok   —— 合法 JSON 但明确非成功（ok:false / timeout_uncertain / 错误对象）→ 走
#               既有确诊/不确定分流；
#   bad_json —— CURL_RC=0（拿到 HTTP 响应，如网关 200）但响应体**不是合法 JSON**
#               （HTML 错误页/代理包装等）→ 消息可能已送达，**不能**记 send_fail（会造成
#               误 down），按「不确定」处理：不退款、不记 send_fail、本 tick 结束（exit 0）
#               下轮自然再试。解析失败体本身不可判断送达与否，保守取「不确定」。
#   transport_fail —— CURL_RC≠0（bridge 不可达/连接失败/超时，无 HTTP 响应）→ 是**确定
#               的发送链故障**，照旧走 send_fail 失败分支（F-A6-2：bridge 挂 → 记 send_fail
#               推进 fail_streak → 达阈值 down 暂停，与「200+非预期体」的 uncertain 区分）。
SEND_JSON_OK="$(printf '%s' "$SEND_RESP" | "$PY" -c 'import json,sys
try:
    ok = json.load(sys.stdin).get("ok")
    print("ok" if ok is True else "not_ok")
except Exception:
    print("bad_json")' 2>/dev/null || echo bad_json)"
if [ "$SEND_JSON_OK" = "ok" ]; then
  : # 发送成功 → 落到下方成功记账分支
else
  # R8 (F-A17-003): bridge 超时不确定（timeout_uncertain）——bot.send 不可取消，
  # 超时不代表未送达。若按失败退款会恢复额度清冷却，制造下次 tick 重发窗口 →
  # 用户可能收到两条重复消息。故：不 refund、不记 send_fail、不重发——
  # 本 tick 直接结束（exit 0），下轮自然再试。这条校验必须在 prepare failed /
  # 普通失败分流之前，避免把"不确定"当"确定失败"。
  if printf '%s' "$SEND_RESP" | grep -q 'timeout_uncertain'; then
    echo "[chiguo-tick] bridge /send 超时且结果不确定（timeout_uncertain）——不退款、不记 send_fail、本 tick 结束下轮再试" >&2
    # RF11 (M2): 轻量清算——持续超时（实际未送达）会累积未回复计数致 silent 禁发。
    # 不 refund（防已送达制造重复消息窗口），但回传 uncertain 从**未回复计数**移除 1 条
    # （record_send_result 的 uncertain 分支：只清 messages_without_reply，不清额度/冷却）。
    [ -z "$MSG_ID" ] || {
      "$PY" "$REPO/chiguo_daemon.py" --send-result "$MSG_ID" --send-status uncertain --error "timeout_uncertain" >/dev/null 2>&1 \
        || echo "[chiguo-tick] send-result(uncertain) 回传失败 msg_id=$MSG_ID" >&2
    }
    exit 0
  fi
  # RF1: 拿到 HTTP 响应（CURL_RC=0）且响应体**非空非 JSON**（HTML 错误页/代理包装）→ 送达
  # 结果不定，按「不确定」退出，与 timeout_uncertain 同策略（不退款不 send_fail，不误 down）。
  # 空响应体 / CURL_RC≠0（bridge 不可达，无 HTTP 响应 / 连不上）不是本分支——那是确定发送链
  # 故障，落下方 send_fail（F-A6-2：bridge 挂 → 记 send_fail 推进 fail_streak → 达阈值 down）。
  if [ "$SEND_JSON_OK" = "bad_json" ] && [ "$CURL_RC" = 0 ] && [ -n "$SEND_RESP" ]; then
    echo "[chiguo-tick] bridge /send 响应非空但非法 JSON（可能被网关/代理包装），发送结果不确定——不退款、不记 send_fail、本 tick 结束下轮再试: $(printf '%s' "$SEND_RESP" | head -c 120)" >&2
    # RF11: 同样轻量清算未回复计数（结果不确定，防未回复累积致 silent）。
    [ -z "$MSG_ID" ] || {
      "$PY" "$REPO/chiguo_daemon.py" --send-result "$MSG_ID" --send-status uncertain --error "non_json_body" >/dev/null 2>&1 \
        || echo "[chiguo-tick] send-result(uncertain) 回传失败 msg_id=$MSG_ID" >&2
    }
    exit 0
  fi
  case "$SEND_RESP" in
    *"prepare failed"*)
      echo "[chiguo-tick] bridge 发送失败: context_token 过期（prepare failed）——从微信给机器人发一条消息即刷新恢复，无需重新扫码（详见 README「认证迁移」）" >&2
      SEND_ERR="context_token expired (prepare failed)"
      ;;
    *)
      echo "[chiguo-tick] bridge 发送失败: ${SEND_RESP:-无响应}，下个 tick 重试" >&2
      SEND_ERR="bridge send failed: $(printf '%s' "$SEND_RESP" | head -c 120)"
      ;;
  esac
  # 回传失败 → daemon 侧 refund_send（energy/quota 回滚 + Hawkes 事件剔除），反馈闭环不静默断
  if [ -n "$MSG_ID" ]; then
    "$PY" "$REPO/chiguo_daemon.py" --send-result "$MSG_ID" --send-status failed --error "$SEND_ERR" >/dev/null 2>&1 \
      || echo "[chiguo-tick] send-result(failed) 回传失败 msg_id=$MSG_ID" >&2
  fi
  # F-A6-2: 发送失败也进健康状态机——生成已 OK，但发送（bridge /send）失败的轮次
  # 视为一次失败，记 send_fail 推进 fail_streak；连续 3 次发送失败 → 达阈值 down +
  # transition 告警 + 暂停，健康语义不再恒 up（与 R-TRO-010b 同点修复：不再发送前清零）。
  # 复用 record_health（失败静默，transition 告警经 /send，bridge 挂时告警也推不出但记账正确）。
  # 注意 record_health 签名是 <outcome> <reason>（内部自带 --reason），勿传 --reason 前缀。
  record_health send_fail "bridge send failed"
  exit 1
fi
# 发送成功 → 记 health success（生成+发送双成功才算健康；down→up 恢复在此翻转）
record_health success
# 回传发送结果（health 已记 success）
# A2: --trigger 来自决策 JSON（sent+1 的归因键），无 trigger（如告警直发）则跳过。
if [ -n "$MSG_ID" ]; then
  RS_ARGS=(--record-send "$MSG_ID" --text "$TEXT")
  [ -n "$TRIGGER" ] && RS_ARGS+=(--trigger "$TRIGGER")
  "$PY" "$REPO/chiguo_daemon.py" "${RS_ARGS[@]}" >/dev/null 2>&1 \
    || echo "[chiguo-tick] record-send 失败 msg_id=$MSG_ID" >&2
fi
