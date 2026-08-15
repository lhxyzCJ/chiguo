#!/usr/bin/env bash
# ============================================================
# chiguo pi-agent 环境安装/校验器（可移植：任意 pull 仓库的机器）
# Phase 4 新架构：LLM 消息生成走 pi-agent（provider 可配，见 [host].provider；opencode-go 为默认示例）
# （ollama embedding）+ 系统 crontab（chiguo-tick）。
# 模式: --dry-run（只扫描报告，只读）/ --yes（自动全部）/ 默认交互 ask（逐项确认；
#       非 TTY 等价 --dry-run）
# 退出码: 0=完成  1=有待办/警告/残留未处理  2=严重问题
# 幂等: 重复运行安全；每次修改前备份。
# 安全边界: 只写 ~/.pi/；阶段 0b 另清理已废弃扩展 memory-lancedb-pro 的 ~/.pi-agent 残留（mem0 唯一后端）。
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHIGUO_REPO="${CHIGUO_REPO_OVERRIDE:-$REPO_DIR}"   # 测试注入点；生产=仓库根
MODE=ask
[ -t 0 ] || MODE=dry-run
for a in "$@"; do
  case "$a" in
    --dry-run) MODE=dry-run ;;
    --yes) MODE=yes ;;
    --skip-agent) exit 0 ;;   # deploy.sh 传参时静默跳过
  esac
done

say() { printf '\033[1;32m[chiguo-agent]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chiguo-agent]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[chiguo-agent]\033[0m %s\n' "$*"; exit 2; }
DRY=0; [ "$MODE" = dry-run ] && DRY=1
PENDING=0    # 1 = 有待办/残留（dry-run 报告；yes/ask 完成后仍存在则退出 1）
PY="$(command -v python3 || echo "$REPO_DIR/.venv/bin/python")"   # JSON 编辑（stdlib 即可）

# provider 单一来源：toml [host].provider（= pi --provider 名与 auth.json 键名；
# 支持 pi 全部内置 provider 与 models.json 注册的自定义 provider；缺省回退 opencode-go）
PROVIDER="$(sed -n 's/^provider *= *"\([^"]*\)".*/\1/p' "$CHIGUO_REPO/chiguo_proactive.toml" | head -1 || true)"
[ -n "$PROVIDER" ] || PROVIDER=opencode-go
# key 来源：AGENT_API_KEY（通用名）优先，OPENCODE_API_KEY 兼容回退
if [ -n "${AGENT_API_KEY:-}" ]; then
  KEY_VAR=AGENT_API_KEY; KEY_VAL="$AGENT_API_KEY"
elif [ -n "${OPENCODE_API_KEY:-}" ]; then
  KEY_VAR=OPENCODE_API_KEY; KEY_VAL="$OPENCODE_API_KEY"
else
  KEY_VAR=OPENCODE_API_KEY; KEY_VAL=""
fi

# 交互模式（ask）：每个写操作前逐项确认；回答 y 执行，n/其他 跳过并把 PENDING=1（残留未处理）
confirm() { [ "$MODE" = ask ] || return 0
  printf '  [ask] %s\n' "$1"
  read -r -p '  执行？[y/N] ' ans
  case "$ans" in
    y|Y|yes|Yes|YES) return 0 ;;
    *) PENDING=1; printf '  [ask] 已跳过（视为残留未处理，最终退出码 1）\n'; return 1 ;;
  esac
}

# auth.json 含 provider 且有真值 key（与 envcheck check_pi_auth 语义一致；
# 裸 grep provider 名会把注释/残缺条目误判为已配置）
auth_has_key() {
  [ -f "$AUTH" ] || return 1
  AUTH_PROVIDER="$PROVIDER" "$PY" - "$AUTH" <<'PYC' >/dev/null 2>&1
import json, os, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
entry = cfg.get(os.environ.get("AUTH_PROVIDER", "opencode-go"))
sys.exit(0 if isinstance(entry, dict) and entry.get("key") else 1)
PYC
}

# ── 固定路径 ───────────────────────────────────────────────
AGENT_BIN="$(command -v pi || true)"
AUTH="$HOME/.pi/agent/auth.json"
TICK="$CHIGUO_REPO/scripts/chiguo-tick.sh"
# cron 把命令段里的 % 当换行符 → 路径中的 % 必须转义为 \%（否则 tick 命令被截断）
CRON_LINE="*/15 * * * * ${TICK//%/\\%} >> ${CHIGUO_REPO//%/\\%}/logs/cron-tick.log 2>&1"
OLLAMA_BASE="${OLLAMA_BASE:-http://localhost:11434}"

# ── 阶段 0: 环境探测 ───────────────────────────────────────
if [ -z "$AGENT_BIN" ]; then
  fail "未检测到 pi → 请先安装 pi-agent（本脚本只配置不安装 pi 本体；Phase 4 消息生成端缺失）"
fi
say "pi $(pi --version 2>&1 | head -1)"
if [ -n "$KEY_VAL" ]; then
  say "$KEY_VAR 已设置 → 阶段 5 可写入 $PROVIDER key"
else
  warn "$KEY_VAR 未设置 → $PROVIDER key 无法写入（auth.json 已有该条目则不受影响）"
fi

# ── 阶段 0b: 清理 pi 记忆扩展残留（memory-lancedb-pro 已移除；mem0 唯一后端）──
# mem0 现为唯一记忆后端，该扩展不再安装/加载；对历史部署机幂等清理，
# 无扩展机器零副作用。dry-run 只报告；ask/yes 执行（ask 模式一次性 confirm）。
cleanup_memory_extension() {
  local SET="$HOME/.pi/agent/settings.json"
  local FOUND=0 HAS_EXT=0
  # 1) settings.json 是否仍引用 memory-lancedb-pro 扩展（只读探测；扩展路径含该串）
  if [ -f "$SET" ] && grep -q 'memory-lancedb-pro' "$SET"; then
    HAS_EXT=1; FOUND=1
    [ "$DRY" = 1 ] && echo "  [dry-run] 将清理 settings.json 的 memory-lancedb-pro 扩展条目"
  fi
  # 2) 文件/目录残留探测
  local t
  for t in \
    "$HOME/.pi/agent/memory-lancedb-pro.json5" \
    "$HOME/.pi/agent/memory-lancedb-pro.json5.bak" \
    "$HOME/.pi/agent/memory" \
    "$HOME/.pi/agent/sessions/--root-.pi-agent-TestForPi-memory-lancedb-pro--" \
    "$HOME/.pi-agent/TestForPi-memory-lancedb-pro" \
    "$HOME/.pi-agent/memory/lancedb-pro"; do
    [ -e "$t" ] || continue
    FOUND=1
    [ "$DRY" = 1 ] && echo "  [dry-run] 将删除记忆扩展残留: $t"
  done
  [ "$FOUND" = 0 ] && return 0
  [ "$DRY" = 1 ] && { PENDING=1; warn "检测到 memory-lancedb-pro 残留 → 执行模式（--yes/ask）将清理"; return 0; }
  if ! confirm "清理 memory-lancedb-pro 扩展残留（settings.json 条目 + json5/会话/数据/代码目录）"; then
    return 0
  fi
  # 3) 实际执行（JSON 保形，先备份；仅实际移除才报成功，解析失败/无匹配条目置 PENDING）
  if [ "$HAS_EXT" = 1 ]; then
    if "$PY" - "$SET" <<'PYC' | grep -q '^removed$'
import json, sys, shutil
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print(f"parse-error: {e}")
    sys.exit(1)
exts = d.get("extensions")
if not isinstance(exts, list):
    sys.exit(0)
keep = []
for e in exts:
    if isinstance(e, str):
        if "memory-lancedb-pro" not in e:
            keep.append(e)
    elif isinstance(e, dict):
        if not any("memory-lancedb-pro" in str(v) for v in e.values()):
            keep.append(e)
    else:
        keep.append(e)
if len(keep) != len(exts):
    shutil.copyfile(sys.argv[1], sys.argv[1] + ".bak-memext")
    if keep:
        d["extensions"] = keep
    else:
        d.pop("extensions", None)
    json.dump(d, open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open(sys.argv[1], "a").write("\n")
    print("removed")
PYC
    then
      say "已从 settings.json 移除 memory-lancedb-pro 扩展条目（备份: settings.json.bak-memext）"
    else
      PENDING=1
      warn "settings.json 清理未生效（解析失败或无匹配条目），请手工检查: $SET"
    fi
  fi
  for t in \
    "$HOME/.pi/agent/memory-lancedb-pro.json5" \
    "$HOME/.pi/agent/memory-lancedb-pro.json5.bak" \
    "$HOME/.pi/agent/memory" \
    "$HOME/.pi/agent/sessions/--root-.pi-agent-TestForPi-memory-lancedb-pro--" \
    "$HOME/.pi-agent/TestForPi-memory-lancedb-pro" \
    "$HOME/.pi-agent/memory/lancedb-pro"; do
    [ -e "$t" ] && { rm -rf "$t"; say "已删除记忆扩展残留: $t"; }
  done
  say "memory-lancedb-pro 扩展残留已清理（mem0 唯一记忆后端）"
}
cleanup_memory_extension

# ── 阶段 4: ollama embedding 模型检查 ──────────────────────
say "阶段 4: ollama embedding（$OLLAMA_BASE）..."
TAGS="$(curl -sf --max-time 5 --noproxy '*' "$OLLAMA_BASE/api/tags" 2>/dev/null || true)"
if printf '%s' "$TAGS" | grep -q 'qwen3-embedding'; then
  say "ollama OK（$OLLAMA_BASE 已有 qwen3-embedding）"
elif [ -z "$TAGS" ]; then
  PENDING=1
  warn "ollama 不可达（$OLLAMA_BASE）→ 记忆 embedding 降级；请启动 ollama（ollama serve）后重跑"
elif [ "$DRY" = 1 ]; then
  PENDING=1
  echo "  [dry-run] ollama 缺 qwen3-embedding:0.6b → 将执行 ollama pull qwen3-embedding:0.6b"
elif confirm "ollama pull qwen3-embedding:0.6b（约 600MB）"; then
  if timeout 600 ollama pull qwen3-embedding:0.6b >/dev/null 2>&1; then
    say "qwen3-embedding:0.6b 已拉取"
  else
    PENDING=1; warn "ollama pull 失败（请手工: ollama pull qwen3-embedding:0.6b）"
  fi
fi

# ── 阶段 5: auth.json $PROVIDER 条目（key 从环境变量读，不落盘明文）──
# 集中认证迁移源：~/.chiguo/auth/agent-auth.json → ~/.pi/agent/auth.json（目标已有则不动，本地为准）
if [ ! -f "$AUTH" ] && [ -f "$HOME/.chiguo/auth/agent-auth.json" ]; then
  mkdir -p "$(dirname "$AUTH")"
  cp -a "$HOME/.chiguo/auth/agent-auth.json" "$AUTH" && chmod 600 "$AUTH" \
    && say "已从 ~/.chiguo/auth/agent-auth.json 导入认证（集中认证目录迁移）"
fi
say "阶段 5: ~/.pi/agent/auth.json $PROVIDER 条目..."
if auth_has_key; then
  say "auth.json OK（已含 $PROVIDER key）"
elif [ -z "$KEY_VAL" ]; then
  PENDING=1
  warn "auth.json 缺 $PROVIDER 且 $KEY_VAR 未设置 → 无法写入 key；export $KEY_VAR=... 后重跑（或手工编辑 $AUTH）"
else
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将写 $PROVIDER 条目到 $AUTH（key 从 $KEY_VAR 读，chmod 600，含 .bak 备份）"
  elif confirm "写入 $AUTH 的 $PROVIDER 条目（key 来自 $KEY_VAR）"; then
    [ -f "$AUTH" ] && cp -a "$AUTH" "$AUTH.bak"
    # key 经环境变量传给 python3（argv 会被 ps 看到，明文泄露面更大）
    if KEY_VAL="$KEY_VAL" AUTH_PROVIDER="$PROVIDER" "$PY" - "$AUTH" <<'PYJ'; then
import json, os, sys
p = sys.argv[1]
key = os.environ["KEY_VAL"]
provider = os.environ.get("AUTH_PROVIDER", "opencode-go")
cfg = {}
if os.path.exists(p):
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
os.makedirs(os.path.dirname(p), exist_ok=True)
cfg[provider] = {"type": "api_key", "key": key}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(p, 0o600)
PYJ
      say "auth.json 已写入 $PROVIDER 条目"
    else
      PENDING=1; warn "auth.json 写入失败（.bak 已保留，请手工处理）"
    fi
  fi
fi

# ── crontab 条目工具：区分「活动旧条目」与「被注释禁用的手动禁用条目」──
# 被注释行（# 开头，可带前导空白）= 用户刻意停用：保留原样，不计入旧条目，
# 绝不静默移除/恢复（issue #147：grep -v 连注释行一起删 → 静默重开被停用的自动推送）。
cron_has_commented() {  # $1=标识符（chiguo-tick/replan-tick）；命中被注释行返回 0
  printf '%s\n' "$CURRENT_CRON" | awk -v k="$1" '$0 ~ k && /^[[:space:]]*#/ { f=1 } END { exit f ? 0 : 1 }'
}
cron_has_active() {  # $1=标识符；命中活动旧条目（非注释行）返回 0
  printf '%s\n' "$CURRENT_CRON" | awk -v k="$1" '$0 ~ k && !/^[[:space:]]*#/ { f=1 } END { exit f ? 0 : 1 }'
}
cron_filter_active() {  # 移除活动旧条目、保留被注释禁用行（stdin → stdout）
  awk -v k="$1" '!($0 ~ k) || /^[[:space:]]*#/'
}
# 被注释禁用条目检测 → 醒目 warn + ask 模式额外 confirm；返回 0=可继续（无注释行，或已确认），
# 1=ask 拒绝（confirm 已置 PENDING，调用方跳过整段处理）
cron_check_commented() {
  cron_has_commented "$1" || return 0
  warn "检测到被注释禁用的 $1 crontab 条目——这是手动禁用，不会被自动移除/恢复"
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 被注释的 $1 条目将保持原样（手动禁用不干预）"
    return 0
  fi
  confirm "检测到被注释禁用的 $1 条目（手动禁用）；确认仍继续处理 crontab？"
}

# ── 阶段 6: crontab 注册 chiguo-tick ───────────────────────
# CHIGUO_DAEMON_LOOP=1 → 决策引擎改 systemd 常驻（--loop，发送侧内聚），
# 跳过 tick 条目（cron tick 与 loop 并存会双发消息，必须互斥）。
if [ "${CHIGUO_DAEMON_LOOP:-0}" = "1" ]; then
  say "CHIGUO_DAEMON_LOOP=1 → 跳过 chiguo-tick crontab（决策引擎改由 chiguo-daemon.service 常驻）"
  CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
  if cron_check_commented chiguo-tick; then
    if cron_has_active chiguo-tick; then
      if [ "$DRY" = 1 ]; then
        PENDING=1
        echo "  [dry-run] 将移除旧 chiguo-tick crontab 条目（防与 loop 常驻双发）"
      elif confirm "移除旧 chiguo-tick crontab 条目（已切换 loop 常驻）"; then
        if printf '%s\n' "$CURRENT_CRON" | cron_filter_active chiguo-tick | crontab -; then
          say "crontab 旧 chiguo-tick 条目已移除（被注释禁用条目保持原样）"
        else
          PENDING=1; warn "crontab 移除失败（请手工执行: crontab -l | awk '!/chiguo-tick/ || /^[[:space:]]*#/' | crontab -）"
        fi
      fi
    else
      say "crontab 无活动 chiguo-tick 条目（loop 模式预期）"
    fi
  fi
else
say "阶段 6: crontab 注册 chiguo-tick..."
# loop→cron 反向切换：cron 模式检测已装的 chiguo-daemon.service 并停用（防双发）
if [ -f "${CHIGUO_SYSTEMD_DIR:-/etc/systemd/system}/chiguo-daemon.service" ]; then
  SYSTEMCTL="${CHIGUO_SYSTEMCTL:-systemctl}"
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 检测到 chiguo-daemon.service（loop 常驻）→ 将停用以切回 cron 形态"
  elif confirm "停用 chiguo-daemon.service（切回 cron 形态，防双发）"; then
    if "$SYSTEMCTL" disable --now chiguo-daemon.service 2>/dev/null; then
      say "chiguo-daemon.service 已停用（cron 形态）"
    else
      PENDING=1; warn "停用失败（请手工执行: systemctl disable --now chiguo-daemon.service）"
    fi
  fi
fi
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
if cron_check_commented chiguo-tick; then
  if printf '%s\n' "$CURRENT_CRON" | grep -Fqx "$CRON_LINE"; then
    say "crontab 已注册 chiguo-tick（$CRON_LINE）"
  elif cron_has_active chiguo-tick; then
    # 旧条目（仓库路径/参数已变，仅 grep 名字发现不了）→ 按当前 CRON_LINE 整行替换
    if [ "$DRY" = 1 ]; then
      PENDING=1
      echo "  [dry-run] crontab 有旧 chiguo-tick 条目（路径已变）→ 将替换为: $CRON_LINE"
    elif confirm "替换旧 chiguo-tick 条目为: $CRON_LINE"; then
      mkdir -p "$CHIGUO_REPO/logs"   # 重定向目标目录：缺目录 → cron 整条命令失败（tick 永不执行）
      if ( printf '%s\n' "$CURRENT_CRON" | cron_filter_active chiguo-tick || true; echo "$CRON_LINE" ) | crontab -; then
        say "crontab 旧条目已替换（被注释禁用条目保持原样）"
      else
        PENDING=1; warn "crontab 替换失败（请手工执行: (crontab -l | awk '!/chiguo-tick/ || /^[[:space:]]*#/'; echo '$CRON_LINE') | crontab -）"
      fi
    fi
  else
    if [ "$DRY" = 1 ]; then
      PENDING=1
      echo "  [dry-run] 将注册 crontab: $CRON_LINE"
    elif confirm "注册 crontab: $CRON_LINE"; then
      mkdir -p "$CHIGUO_REPO/logs"   # 重定向目标目录：缺目录 → cron 整条命令失败（tick 永不执行）
      if ( printf '%s\n' "$CURRENT_CRON"; echo "$CRON_LINE" ) | crontab -; then
        say "crontab 已注册"
      else
        PENDING=1; warn "crontab 注册失败（请手工执行: (crontab -l; echo '$CRON_LINE') | crontab -）"
      fi
    fi
  fi
fi
fi

# ── 阶段 6b: crontab 注册 replan-tick（幂等,与 tick 条目同款逻辑）──
# CURRENT_CRON 必须重读:tick 条目刚在阶段 6 写入,旧快照会致 replan 追加时覆盖整表
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
# cron 把命令段里的 % 当换行符 → 路径中的 % 必须转义为 \%
REPLAN_LINE="*/15 * * * * ${CHIGUO_REPO//%/\\%}/scripts/replan-tick.sh >> ${CHIGUO_REPO//%/\\%}/logs/cron-replan.log 2>&1"
if cron_check_commented replan-tick; then
  if printf '%s\n' "$CURRENT_CRON" | grep -Fqx "$REPLAN_LINE"; then
    say "crontab 已注册 replan-tick"
  elif cron_has_active replan-tick; then
    if [ "$DRY" = 1 ]; then
      PENDING=1
      echo "  [dry-run] crontab 有旧 replan-tick 条目（路径已变）→ 将替换为: $REPLAN_LINE"
    elif confirm "替换旧 replan-tick 条目为: $REPLAN_LINE"; then
      mkdir -p "$CHIGUO_REPO/logs"
      if ( printf '%s\n' "$CURRENT_CRON" | cron_filter_active replan-tick || true; echo "$REPLAN_LINE" ) | crontab -; then
        say "crontab 旧 replan-tick 条目已替换（被注释禁用条目保持原样）"
      else
        PENDING=1; warn "crontab replan 替换失败（请手工执行: (crontab -l | awk '!/replan-tick/ || /^[[:space:]]*#/'; echo '$REPLAN_LINE') | crontab -）"
      fi
    fi
  else
    if [ "$DRY" = 1 ]; then
      PENDING=1
      echo "  [dry-run] 将注册 crontab: $REPLAN_LINE"
    elif confirm "注册 replan crontab: $REPLAN_LINE"; then
      mkdir -p "$CHIGUO_REPO/logs"
      if ( printf '%s\n' "$CURRENT_CRON"; echo "$REPLAN_LINE" ) | crontab -; then
        say "crontab 已注册 replan-tick"
      else
        PENDING=1; warn "crontab replan 注册失败（请手工执行: (crontab -l; echo '$REPLAN_LINE') | crontab -）"
      fi
    fi
  fi
fi

# ── 阶段 6c: systemd chiguo-daemon.service（CHIGUO_DAEMON_LOOP=1 时安装）──
if [ "${CHIGUO_DAEMON_LOOP:-0}" = "1" ]; then
  SYSTEMD_DIR="${CHIGUO_SYSTEMD_DIR:-/etc/systemd/system}"
  SYSTEMCTL="${CHIGUO_SYSTEMCTL:-systemctl}"
  DAEMON_UNIT="$SYSTEMD_DIR/chiguo-daemon.service"
  UNIT_PY="$CHIGUO_REPO/.venv/bin/python"  # ExecStart 用仓库 venv python（含 mem0 等依赖）；缺失时 systemd 启动即失败，envcheck 会指出
  [ -x "$UNIT_PY" ] || warn "$UNIT_PY 不存在（请先 bash deploy.sh 完成基础安装）"
  say "阶段 6c: 安装 systemd chiguo-daemon.service（--loop 900 --compact 常驻）..."
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将写入 $DAEMON_UNIT（ExecStart=$UNIT_PY $CHIGUO_REPO/chiguo_daemon.py --loop 900 --compact）"
  else
    TMP_UNIT="$(mktemp "${TMPDIR:-/tmp}/chiguo-daemon-XXXXXX.service")"
    # unit 模板始终取自脚本所在仓库（CHIGUO_REPO 可能被测试 override）
    # 占位符替换走 Python str.replace：sed 替换串里的 & / \ | 会被误解释
    # 解释器用 $PY（第 31 行的系统 python3），ExecStart 值用 $UNIT_PY（仓库 venv python）
    "$PY" - "$REPO_DIR/scripts/chiguo-daemon.service" "$UNIT_PY" "$CHIGUO_REPO" <<'PYU' > "$TMP_UNIT"
import sys
src, py, repo = sys.argv[1], sys.argv[2], sys.argv[3]
sys.stdout.write(open(src, encoding="utf-8").read().replace("__PY__", py).replace("__REPO__", repo))
PYU
    if [ -f "$DAEMON_UNIT" ] && cmp -s "$TMP_UNIT" "$DAEMON_UNIT"; then
      say "chiguo-daemon.service 已是最新（跳过写入）"
      rm -f "$TMP_UNIT"
    elif confirm "写入 $DAEMON_UNIT 并启用 chiguo-daemon.service（loop 常驻）"; then
      # M-2: loop 形态依赖 bridge token——缺失时 /send 全 403 静默丢主动消息。
      # enable 前校验 .env 或 toml [loop].bridge_token 有非空 token，缺失则告警跳过。
      BRIDGE_ENV="$CHIGUO_REPO/wechat-bridge/.env"
      LOOP_TOKEN_CFG="$(sed -n 's/^[[:space:]]*bridge_token[[:space:]]*=*[[:space:]]*"\([^"]*\)".*/\1/p' "$CHIGUO_REPO/chiguo_proactive.toml" 2>/dev/null | head -1 || true)"
      ENV_TOKEN="$(sed -n 's/^WECHAT_BRIDGE_TOKEN=//p' "$BRIDGE_ENV" 2>/dev/null | head -1 || true)"
      if [ -z "$ENV_TOKEN" ] && [ -z "$LOOP_TOKEN_CFG" ]; then
        PENDING=1; warn "loop 常驻未配置 bridge token(.env 缺 WECHAT_BRIDGE_TOKEN 且 toml [loop].bridge_token 为空)→ /send 将 403;请先 bash deploy.sh 装 bridge 或配置 token 后再启用"
        rm -f "$TMP_UNIT"
      else
        mkdir -p "$SYSTEMD_DIR"
        mv "$TMP_UNIT" "$DAEMON_UNIT"
        if "$SYSTEMCTL" daemon-reload && "$SYSTEMCTL" enable --now chiguo-daemon.service; then
          say "chiguo-daemon.service 已启用并启动（loop 常驻；cron tick 已互斥跳过）"
        else
          PENDING=1; warn "systemctl 启用失败（请手工执行: systemctl daemon-reload && systemctl enable --now chiguo-daemon.service）"
        fi
      fi
    else
      rm -f "$TMP_UNIT"
    fi
  fi
fi

# ── 阶段 7: 冒烟验证（仅 --yes/ask；dry-run 不执行任何命令）──
say "阶段 7: 冒烟验证..."
if [ "$DRY" = 1 ]; then
  say "dry-run 不执行冒烟（pi 实调留给 --yes 或 Task 15 集成冒烟）"
elif [ "$MODE" = ask ] && ! confirm "执行冒烟（pi 实调，key 可用时）"; then
  :
else
  SMOKE_BAD=0
  if auth_has_key; then
    MODEL="$(sed -n 's/^model *= *"\([^"]*\)".*/\1/p' "$CHIGUO_REPO/chiguo_proactive.toml" | head -1)"
    [ -n "$MODEL" ] || MODEL=deepseek-v4-flash
    if SMOKE_OUT="$(timeout 120 "$AGENT_BIN" -p --provider "$PROVIDER" --model "$MODEL" \
        --session-id chiguo-install-smoke --no-context-files \
        --thinking high --mode json '回复:ok' 2>&1)" \
       && printf '%s' "$SMOKE_OUT" | grep -q 'message_end'; then
      say "pi 冒烟 OK（provider=$PROVIDER model=$MODEL）"
    else
      SMOKE_BAD=1; warn "pi 冒烟失败: $(printf '%s' "$SMOKE_OUT" | tail -c 300 | tr '\n' ' ')"
    fi
  else
    warn "无 $PROVIDER key → 跳过 pi 冒烟（设置 $KEY_VAR 后重跑）"
  fi
  [ "$SMOKE_BAD" = 1 ] && PENDING=1
fi

# ── 收尾 ───────────────────────────────────────────────────
if [ "$DRY" = 1 ]; then
  [ "$PENDING" = 1 ] && { say "dry-run：有待办（见上）"; exit 1; }
  say "dry-run：无待办（全部已安装）"
  exit 0
fi
[ "$PENDING" = 1 ] && { warn "存在待办/残留未处理"; exit 1; }
say "agent 后端安装完成 ✓"
exit 0
