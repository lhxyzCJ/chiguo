#!/usr/bin/env bash
# ============================================================
# chiguo pi-agent 环境安装/校验器（可移植：任意 pull 仓库的机器）
# Phase 4 新架构：LLM 消息生成走 pi-agent（provider 可配，见 [host].provider；opencode-go 为默认示例）
# （ollama embedding）+ 系统 crontab（chiguo-tick）。
# 模式: --dry-run（只扫描报告，只读）/ --yes（自动全部）/ 默认交互 ask（逐项确认；
#       非 TTY 等价 --dry-run）
# 退出码: 0=完成  1=有待办/警告/残留未处理  2=严重问题
# 幂等: 重复运行安全；每次修改前备份。
# 安全边界: 只写 ~/.pi/。
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
CRON_LINE="*/15 * * * * $TICK >> $CHIGUO_REPO/logs/cron-tick.log 2>&1"
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

# ── 阶段 6: crontab 注册 chiguo-tick ───────────────────────
# v1.11 C: CHIGUO_DAEMON_LOOP=1 → 决策引擎改 systemd 常驻（--loop，发送侧内聚），
# 跳过 tick 条目（cron tick 与 loop 并存会双发消息，必须互斥）。
if [ "${CHIGUO_DAEMON_LOOP:-0}" = "1" ]; then
  say "CHIGUO_DAEMON_LOOP=1 → 跳过 chiguo-tick crontab（决策引擎改由 chiguo-daemon.service 常驻）"
  CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
  if printf '%s\n' "$CURRENT_CRON" | grep -q 'chiguo-tick'; then
    if [ "$DRY" = 1 ]; then
      PENDING=1
      echo "  [dry-run] 将移除旧 chiguo-tick crontab 条目（防与 loop 常驻双发）"
    elif confirm "移除旧 chiguo-tick crontab 条目（已切换 loop 常驻）"; then
      if printf '%s\n' "$CURRENT_CRON" | grep -v 'chiguo-tick' | crontab -; then
        say "crontab 旧 chiguo-tick 条目已移除"
      else
        PENDING=1; warn "crontab 移除失败（请手工执行: crontab -l | grep -v chiguo-tick | crontab -）"
      fi
    fi
  else
    say "crontab 无 chiguo-tick 条目（loop 模式预期）"
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
if printf '%s\n' "$CURRENT_CRON" | grep -Fqx "$CRON_LINE"; then
  say "crontab 已注册 chiguo-tick（$CRON_LINE）"
elif printf '%s\n' "$CURRENT_CRON" | grep -q 'chiguo-tick'; then
  # 旧条目（仓库路径/参数已变，仅 grep 名字发现不了）→ 按当前 CRON_LINE 整行替换
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] crontab 有旧 chiguo-tick 条目（路径已变）→ 将替换为: $CRON_LINE"
  elif confirm "替换旧 chiguo-tick 条目为: $CRON_LINE"; then
    mkdir -p "$CHIGUO_REPO/logs"   # 重定向目标目录：缺目录 → cron 整条命令失败（tick 永不执行）
    if ( printf '%s\n' "$CURRENT_CRON" | grep -v 'chiguo-tick' || true; echo "$CRON_LINE" ) | crontab -; then
      say "crontab 旧条目已替换"
    else
      PENDING=1; warn "crontab 替换失败（请手工执行: (crontab -l | grep -v chiguo-tick; echo '$CRON_LINE') | crontab -）"
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

# ── 阶段 6b: crontab 注册 replan-tick（幂等,与 tick 条目同款逻辑）──
# CURRENT_CRON 必须重读:tick 条目刚在阶段 6 写入,旧快照会致 replan 追加时覆盖整表
CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
REPLAN_LINE="*/15 * * * * $CHIGUO_REPO/scripts/replan-tick.sh >> $CHIGUO_REPO/logs/cron-replan.log 2>&1"
if printf '%s\n' "$CURRENT_CRON" | grep -Fqx "$REPLAN_LINE"; then
  say "crontab 已注册 replan-tick"
elif printf '%s\n' "$CURRENT_CRON" | grep -q 'replan-tick'; then
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] crontab 有旧 replan-tick 条目（路径已变）→ 将替换为: $REPLAN_LINE"
  elif confirm "替换旧 replan-tick 条目为: $REPLAN_LINE"; then
    mkdir -p "$CHIGUO_REPO/logs"
    if ( printf '%s\n' "$CURRENT_CRON" | grep -v 'replan-tick' || true; echo "$REPLAN_LINE" ) | crontab -; then
      say "crontab 旧 replan-tick 条目已替换"
    else
      PENDING=1; warn "crontab replan 替换失败（请手工执行: (crontab -l | grep -v replan-tick; echo '$REPLAN_LINE') | crontab -）"
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

# ── 阶段 6c: systemd chiguo-daemon.service（CHIGUO_DAEMON_LOOP=1 时安装）──
if [ "${CHIGUO_DAEMON_LOOP:-0}" = "1" ]; then
  SYSTEMD_DIR="${CHIGUO_SYSTEMD_DIR:-/etc/systemd/system}"
  SYSTEMCTL="${CHIGUO_SYSTEMCTL:-systemctl}"
  DAEMON_UNIT="$SYSTEMD_DIR/chiguo-daemon.service"
  PY="$CHIGUO_REPO/.venv/bin/python"  # .venv 由 deploy.sh 保证；缺失时 systemd 启动即失败，envcheck 会指出
  [ -x "$PY" ] || warn "$PY 不存在（请先 bash deploy.sh 完成基础安装）"
  say "阶段 6c: 安装 systemd chiguo-daemon.service（--loop 900 --compact 常驻）..."
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将写入 $DAEMON_UNIT（ExecStart=$PY $CHIGUO_REPO/chiguo_daemon.py --loop 900 --compact）"
  else
    TMP_UNIT="$(mktemp "${TMPDIR:-/tmp}/chiguo-daemon-XXXXXX.service")"
    # unit 模板始终取自脚本所在仓库（CHIGUO_REPO 可能被测试 override）
    sed -e "s|__PY__|$PY|g" -e "s|__REPO__|$CHIGUO_REPO|g" \
      "$REPO_DIR/scripts/chiguo-daemon.service" > "$TMP_UNIT"
    if [ -f "$DAEMON_UNIT" ] && cmp -s "$TMP_UNIT" "$DAEMON_UNIT"; then
      say "chiguo-daemon.service 已是最新（跳过写入）"
      rm -f "$TMP_UNIT"
    elif confirm "写入 $DAEMON_UNIT 并启用 chiguo-daemon.service（loop 常驻）"; then
      mkdir -p "$SYSTEMD_DIR"
      mv "$TMP_UNIT" "$DAEMON_UNIT"
      if "$SYSTEMCTL" daemon-reload && "$SYSTEMCTL" enable --now chiguo-daemon.service; then
        say "chiguo-daemon.service 已启用并启动（loop 常驻；cron tick 已互斥跳过）"
      else
        PENDING=1; warn "systemctl 启用失败（请手工执行: systemctl daemon-reload && systemctl enable --now chiguo-daemon.service）"
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
