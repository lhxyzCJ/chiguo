#!/usr/bin/env bash
# ============================================================
# chiguo pi-agent 环境安装/校验器（可移植：任意 pull 仓库的机器）
# Phase 4 新架构：LLM 消息生成走 pi-agent（opencode-go provider）+ memory-lancedb-pro
# （pi 版扩展，ollama embedding）+ 系统 crontab（chiguo-tick）。
# 模式: --dry-run（只扫描报告，只读）/ --yes（自动全部）/ 默认交互 ask（逐项确认；
#       非 TTY 等价 --dry-run）
# 退出码: 0=完成  1=有待办/警告/残留未处理  2=严重问题
# 幂等: 重复运行安全；每次修改前备份。
# 安全边界: 只写 ~/.pi/ 与 ~/.pi-agent/；记忆库位于 ~/.pi-agent/memory/lancedb-pro
#       （OpenClaw 已停用，历史记忆库迁出，不修改）。
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
    --skip-pi) exit 0 ;;   # deploy.sh 传参时静默跳过
  esac
done

say() { printf '\033[1;32m[chiguo-pi]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chiguo-pi]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[chiguo-pi]\033[0m %s\n' "$*"; exit 2; }
DRY=0; [ "$MODE" = dry-run ] && DRY=1
PENDING=0    # 1 = 有待办/残留（dry-run 报告；yes/ask 完成后仍存在则退出 1）
PY="$(command -v python3 || echo "$REPO_DIR/.venv/bin/python")"   # JSON 编辑（stdlib 即可）

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
# 裸 grep 'opencode-go' 会把注释/残缺条目误判为已配置）
auth_has_key() {
  [ -f "$AUTH" ] || return 1
  "$PY" - "$AUTH" <<'PYC' >/dev/null 2>&1
import json, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
entry = cfg.get("opencode-go")
sys.exit(0 if isinstance(entry, dict) and entry.get("key") else 1)
PYC
}

# ── 固定路径 ───────────────────────────────────────────────
PI_BIN="$(command -v pi || true)"
EXT_DIR="$HOME/.pi-agent"
CLONE="$EXT_DIR/TestForPi-memory-lancedb-pro"
EXT_JS="$CLONE/dist/pi-adapter/index.js"
SETTINGS="$HOME/.pi/agent/settings.json"
JSON5="$HOME/.pi/agent/memory-lancedb-pro.json5"
AUTH="$HOME/.pi/agent/auth.json"
TICK="$CHIGUO_REPO/scripts/chiguo-tick.sh"
CRON_LINE="*/15 * * * * $TICK >> $CHIGUO_REPO/logs/cron-tick.log 2>&1"
OLLAMA_BASE="${OLLAMA_BASE:-http://localhost:11434}"

# ── 阶段 0: 环境探测 ───────────────────────────────────────
if [ -z "$PI_BIN" ]; then
  fail "未检测到 pi → 请先安装 pi-agent（本脚本只配置不安装 pi 本体；Phase 4 消息生成端缺失）"
fi
say "pi $(pi --version 2>&1 | head -1)"
if [ -n "${OPENCODE_API_KEY:-}" ]; then
  say "OPENCODE_API_KEY 已设置 → 阶段 5 可写入 opencode-go key"
else
  warn "OPENCODE_API_KEY 未设置 → opencode-go key 无法写入（auth.json 已有该条目则不受影响）"
fi

# ── 阶段 1: memory-lancedb-pro（clone + build）─────────────
say "阶段 1: memory-lancedb-pro 扩展（$CLONE）..."
if [ ! -d "$CLONE" ]; then
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将 git clone https://github.com/lhxyzCJ/TestForPi-memory-lancedb-pro → $CLONE，然后 npm install && npm run build"
  elif confirm "clone memory-lancedb-pro 到 $CLONE 并构建（npm install && npm run build）"; then
    for t in git node npm; do command -v "$t" >/dev/null 2>&1 || fail "缺 $t → 无法 clone/build memory-lancedb-pro"; done
    if git clone --depth 1 https://github.com/lhxyzCJ/TestForPi-memory-lancedb-pro "$CLONE" \
       && ( cd "$CLONE" && npm install && npm run build ); then
      say "clone + build OK（$EXT_JS）"
    else
      PENDING=1; warn "clone/build 失败（残留未处理，请手工处理）"
    fi
  fi
elif [ ! -f "$EXT_JS" ]; then
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] $CLONE 已存在但缺 dist/pi-adapter/index.js → 将 npm install && npm run build"
  elif confirm "在 $CLONE 执行 npm install && npm run build"; then
    for t in node npm; do command -v "$t" >/dev/null 2>&1 || fail "缺 $t → 无法构建 memory-lancedb-pro"; done
    if ( cd "$CLONE" && npm install && npm run build ); then
      say "build OK（$EXT_JS）"
    else
      PENDING=1; warn "build 失败（残留未处理，请手工处理）"
    fi
  fi
else
  say "扩展已 clone + build（$EXT_JS 存在）"
fi

# ── 阶段 2: settings.json extensions（修正 Windows 残留路径）──
say "阶段 2: ~/.pi/agent/settings.json extensions..."
if [ -f "$SETTINGS" ] && "$PY" - "$SETTINGS" "$EXT_JS" <<'PYC' >/dev/null 2>&1; then
import json, sys
p, want = sys.argv[1], sys.argv[2]
with open(p, encoding="utf-8") as f:
    cfg = json.load(f)
exts = cfg.get("extensions") or []
def is_windows(e):
    return (isinstance(e, str)
            and ("/mnt/" in e or "\\" in e
                 or (len(e) > 1 and e[1] == ":" and e[0].isalpha())))
sys.exit(0 if (want in exts and not any(is_windows(e) for e in exts)) else 1)
PYC
  say "settings.json OK（extensions 含 $EXT_JS，无 Windows 残留）"
else
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将修正 $SETTINGS：extensions 写 $EXT_JS（清除 /mnt/c/ 等 Windows 残留路径，含 .bak 备份）"
  elif confirm "修正 $SETTINGS 的 extensions（清除 Windows 残留，写 $EXT_JS）"; then
    [ -f "$SETTINGS" ] && cp -a "$SETTINGS" "$SETTINGS.bak"
    if "$PY" - "$SETTINGS" "$EXT_JS" <<'PYJ'; then
import json, os, sys
p, want = sys.argv[1], sys.argv[2]
cfg = {}
if os.path.exists(p):
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
def is_windows(e):
    return (isinstance(e, str)
            and ("/mnt/" in e or "\\" in e
                 or (len(e) > 1 and e[1] == ":" and e[0].isalpha())))
exts = [e for e in (cfg.get("extensions") or []) if not is_windows(e)]
if want not in exts:
    exts.append(want)
cfg["extensions"] = exts
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
PYJ
      say "settings.json 已修正"
    else
      PENDING=1; warn "settings.json 修正失败（.bak 已保留，请手工处理）"
    fi
  fi
fi

# ── 阶段 3: memory-lancedb-pro.json5（dbPath + ollama embedding）──
say "阶段 3: ~/.pi/agent/memory-lancedb-pro.json5..."
if [ -f "$JSON5" ] \
   && grep -q 'qwen3-embedding' "$JSON5" \
   && grep -q 'localhost:11434' "$JSON5" \
   && grep -q '\.pi-agent/memory/lancedb-pro' "$JSON5" \
   && grep -qE '"autoCapture"[[:space:]]*:[[:space:]]*true' "$JSON5" \
   && grep -qE '"autoRecall"[[:space:]]*:[[:space:]]*true' "$JSON5" \
   && grep -qE '"smartExtraction"[[:space:]]*:[[:space:]]*true' "$JSON5"; then
  say "memory-lancedb-pro.json5 OK（embedding=ollama qwen3-embedding:0.6b + dbPath=~/.pi-agent/memory/lancedb-pro）"
else
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将写 $JSON5：dbPath=~/.pi-agent/memory/lancedb-pro、embedding=ollama qwen3-embedding:0.6b、llm=deepseek、autoCapture/autoRecall/smartExtraction（含 .bak 备份）"
  elif confirm "写入 $JSON5（memory-lancedb-pro 配置：dbPath 沿用历史库 + ollama embedding）"; then
    mkdir -p "$(dirname "$JSON5")"
    [ -f "$JSON5" ] && cp -a "$JSON5" "$JSON5.bak"
    cat > "$JSON5" <<'EOJ'
{
  // memory-lancedb-pro configuration for the pi coding agent (install_pi.sh 生成)
  // embedding: local Ollama (qwen3-embedding:0.6b, 1024 dims)
  // llm: DeepSeek (smart extraction / upgrades)
  "dbPath": "~/.pi-agent/memory/lancedb-pro",
  "embedding": {
    "provider": "openai-compatible",
    "apiKey": "ollama",
    "model": "qwen3-embedding:0.6b",
    "baseURL": "http://localhost:11434/v1",
    "dimensions": 1024,
    "normalized": true
  },
  "llm": {
    "apiKey": "${OPENCODE_API_KEY}",
    "model": "deepseek-v4-flash",
    "baseURL": "https://opencode.ai/zen/go/v1"
  },
  "autoCapture": true,
  "autoRecall": true,
  "smartExtraction": true,
  "extractMinMessages": 2,
  "extractMaxChars": 8000,
  "retrieval": { "mode": "hybrid", "vectorWeight": 0.7, "bm25Weight": 0.3, "minScore": 0.3, "rerank": "none" },
  "sessionMemory": { "enabled": false }
}
EOJ
    say "memory-lancedb-pro.json5 已写入"
  fi
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

# ── 阶段 5: auth.json opencode-go 条目（key 从环境变量读，不落盘明文）──
say "阶段 5: ~/.pi/agent/auth.json opencode-go 条目..."
if auth_has_key; then
  say "auth.json OK（已含 opencode-go key）"
elif [ -z "${OPENCODE_API_KEY:-}" ]; then
  PENDING=1
  warn "auth.json 缺 opencode-go 且 OPENCODE_API_KEY 未设置 → 无法写入 key；export OPENCODE_API_KEY=... 后重跑（或手工编辑 $AUTH）"
else
  if [ "$DRY" = 1 ]; then
    PENDING=1
    echo "  [dry-run] 将写 opencode-go 条目到 $AUTH（key 从 OPENCODE_API_KEY 读，chmod 600，含 .bak 备份）"
  elif confirm "写入 $AUTH 的 opencode-go 条目（key 来自 OPENCODE_API_KEY）"; then
    [ -f "$AUTH" ] && cp -a "$AUTH" "$AUTH.bak"
    # key 经环境变量传给 python3（argv 会被 ps 看到，明文泄露面更大）
    if OPENCODE_API_KEY="${OPENCODE_API_KEY:-}" "$PY" - "$AUTH" <<'PYJ'; then
import json, os, sys
p = sys.argv[1]
key = os.environ["OPENCODE_API_KEY"]
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
cfg["opencode-go"] = {"type": "api_key", "key": key}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(p, 0o600)
PYJ
      say "auth.json 已写入 opencode-go 条目"
    else
      PENDING=1; warn "auth.json 写入失败（.bak 已保留，请手工处理）"
    fi
  fi
fi

# ── 阶段 6: crontab 注册 chiguo-tick ───────────────────────
say "阶段 6: crontab 注册 chiguo-tick..."
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

# ── 阶段 7: 冒烟验证（仅 --yes/ask；dry-run 不执行任何命令）──
say "阶段 7: 冒烟验证..."
if [ "$DRY" = 1 ]; then
  say "dry-run 不执行冒烟（memory-pro stats / pi 实调留给 --yes 或 Task 15 集成冒烟）"
elif [ "$MODE" = ask ] && ! confirm "执行冒烟（memory-pro stats + pi 实调，key 可用时）"; then
  :
else
  SMOKE_BAD=0
  # memory-pro bin 链接可能未生成（npm allow-scripts 拦截）→ 直接 node 跑 cli-main.js
  MEMORY_PRO="$CLONE/node_modules/.bin/memory-pro"
  [ -x "$MEMORY_PRO" ] || MEMORY_PRO="node $CLONE/dist/pi-adapter/cli-main.js"
  if timeout 60 bash -c "$MEMORY_PRO stats" >/dev/null 2>&1; then
    say "memory-pro stats OK（记忆库 ~/.pi-agent/memory/lancedb-pro 可读）"
  else
    SMOKE_BAD=1; warn "memory-pro stats 失败（见上方错误）"
  fi
  if auth_has_key; then
    PROVIDER="$(sed -n 's/^provider *= *"\([^"]*\)".*/\1/p' "$CHIGUO_REPO/chiguo_proactive.toml" | head -1)"
    MODEL="$(sed -n 's/^model *= *"\([^"]*\)".*/\1/p' "$CHIGUO_REPO/chiguo_proactive.toml" | head -1)"
    [ -n "$PROVIDER" ] || PROVIDER=opencode-go
    [ -n "$MODEL" ] || MODEL=deepseek-v4-flash
    if SMOKE_OUT="$(timeout 120 "$PI_BIN" -p --provider "$PROVIDER" --model "$MODEL" \
        --session-id chiguo-install-smoke --no-context-files \
        --thinking high --mode json '回复:ok' 2>&1)" \
       && printf '%s' "$SMOKE_OUT" | grep -q 'message_end'; then
      say "pi 冒烟 OK（provider=$PROVIDER model=$MODEL）"
    else
      SMOKE_BAD=1; warn "pi 冒烟失败: $(printf '%s' "$SMOKE_OUT" | tail -c 300 | tr '\n' ' ')"
    fi
  else
    warn "无 opencode-go key → 跳过 pi 冒烟（设置 OPENCODE_API_KEY 后重跑）"
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
say "pi 环境安装完成 ✓"
exit 0
