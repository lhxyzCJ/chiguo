#!/usr/bin/env bash
# ============================================================
# chiguo OpenClaw 集成安装/校验器（可移植：任意 pull 仓库的机器）
# 依据官方文档 docs.openclaw.ai（出处见 doc/OPENCLAW_INTEGRATION.md §九）
# 模式: --dry-run（只扫描报告）/ --yes（自动全部）/ 默认交互（非 TTY 等价 --dry-run）
# 退出码: 0=完成  1=有待办/警告/残留未处理  2=严重问题
# 幂等: 重复运行安全；每次修改前备份。
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
    --skip-integration) exit 0 ;;   # deploy.sh 传参时静默跳过
  esac
done

say() { printf '\033[1;32m[chiguo-integ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chiguo-integ]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[chiguo-integ]\033[0m %s\n' "$*"; exit 2; }
DRY=0; [ "$MODE" = dry-run ] && DRY=1
PENDING=0    # 1 = 有待办/残留（dry-run 报告；yes/ask 完成后仍存在则退出 1）
PY="$(command -v python3 || echo "$REPO_DIR/.venv/bin/python")"   # JSON 编辑（stdlib 即可）

would() { [ "$DRY" = 1 ] && { PENDING=1; printf '  [dry-run] %s\n' "$*"; } || eval "$*"; }

# ── 阶段 0: 环境探测（官方：<command> --help 为权威清单）──────
if ! command -v openclaw >/dev/null 2>&1; then
  say "未检测到 openclaw → 跳过集成安装（daemon 独立可用；装好 OpenClaw 后重跑本脚本）"
  exit 0
fi
say "openclaw $(openclaw -V 2>&1 | head -1)"
if ! openclaw automations add --help 2>&1 | grep -q -- '--trigger-script'; then
  warn "当前版本不支持 automations --trigger-script（官方：<command> --help 为权威清单）"
  warn "降级路径：保留旧 cron system-event 方式 → 见 doc/OPENCLAW_INTEGRATION.md §八"
  exit 1
fi
say "功能探测通过：支持 --trigger-script"
if ! openclaw gateway status >/dev/null 2>&1; then
  warn "Gateway 状态未知/未运行：trigger 脚本由 Gateway 调度器执行，请先启动（openclaw gateway start）"
fi

# ── 阶段 0b: 旧方案残留扫描（发现即报告）──────────────────
echo "[chiguo-integ] 扫描旧方案残留 ..."
OLD_JOBS="$(openclaw automations list --all 2>/dev/null | grep -i chiguo | grep -vx 'chiguo-check' || true)"
CLAUDE_SETTINGS="$CHIGUO_REPO/.claude/settings.json"
OLD_HOOK=0; [ -f "$CLAUDE_SETTINGS" ] && grep -q 'chiguo' "$CLAUDE_SETTINGS" && OLD_HOOK=1
ON_USER_MSG="$HOME/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh"
CHIGUO_NATIVE_HOOKS="$(openclaw hooks list 2>/dev/null | grep -i chiguo || true)"
LEGACY_HANDLERS="$(openclaw config get hooks.internal.handlers 2>/dev/null || true)"
TRIGGERS_ENABLED="$(openclaw config get cron.triggers.enabled 2>/dev/null || true)"
[ -n "$OLD_JOBS" ] && warn "发现旧 automations 作业: $(echo "$OLD_JOBS" | tr '\n' ' ')"
[ "$OLD_HOOK" = 1 ] && warn "发现 .claude/settings.json 中 chiguo 的 UserPromptSubmit hook"
[ -f "$ON_USER_MSG" ] && warn "发现旧 hook 脚本: $ON_USER_MSG"
[ -n "$CHIGUO_NATIVE_HOOKS" ] && warn "发现 OpenClaw 原生 chiguo hook: $(echo "$CHIGUO_NATIVE_HOOKS" | tr '\n' ' ')"
[ -n "$LEGACY_HANDLERS" ] && warn "发现 legacy hooks.internal.handlers 配置（官方建议迁移）"

# ── 阶段 1: 配置开关（官方入口 config set + validate）───────
if [ "$TRIGGERS_ENABLED" != "true" ]; then
  echo "[chiguo-integ] 开启 cron.triggers.enabled（官方危险自动化开关：脚本以 agent 权限无头执行；本安装器仅注册 chiguo-watch.js 一条命令）"
  would "openclaw config set cron.triggers.enabled true"
else
  say "cron.triggers.enabled 已开启"
fi

# ── 阶段 2: 作业注册（幂等；先清旧作业）──────────────────
for job in $OLD_JOBS; do
  echo "[chiguo-integ] 移除旧作业 $job（由新 trigger-script 作业接管）"
  would "openclaw automations rm $job"
done
if ! openclaw automations get chiguo-check >/dev/null 2>&1; then
  INSTRUCTION="收到迟菓决策结果。按 SUN2.md 人格生成 1-3 句微信消息发给主人（当前微信会话上下文）。遵守 context.layer_guidance 语气指引与 context.instruction 格式约束；layer_guidance 含【安全阀】标记时语气务必温和克制。发送后运行 ${CHIGUO_REPO}/.venv/bin/python ${CHIGUO_REPO}/chiguo_daemon.py --record-send <msg_id> --text <消息原文> --trigger <trigger> --intensity <intensity>；发送失败则运行 --send-result <msg_id> --send-status failed。"
  would "openclaw automations add --name chiguo-check --every 15m --trigger-script '$CHIGUO_REPO/scripts/chiguo-watch.js' --session main --wake now --timeout-seconds 120 --system-event '$INSTRUCTION'"
else
  say "作业 chiguo-check 已存在，跳过注册"
fi

# ── 阶段 3: 回复侧 standing order（幂等标记段）────────────
SO_FILE="$HOME/.openclaw/workspace/agents/main/AGENTS.md"
if ! grep -qs 'CHIGUO-STANDING-ORDER-START' "$SO_FILE" 2>/dev/null; then
  if [ "$DRY" = 1 ]; then
    PENDING=1; echo "  [dry-run] 将写入 standing order 到 $SO_FILE"
  else
    mkdir -p "$(dirname "$SO_FILE")"
    [ -f "$SO_FILE" ] && cp -a "$SO_FILE" "$SO_FILE.bak"
    awk '/# CHIGUO-STANDING-ORDER-START/{f=1;next}/# CHIGUO-STANDING-ORDER-END/{f=0;next}!f' "$SO_FILE" 2>/dev/null > "$SO_FILE.tmp"
    cat >> "$SO_FILE.tmp" <<EOSO

# CHIGUO-STANDING-ORDER-START
## 迟菓消息流程（standing order，每会话注入）
每次收到主人的微信消息并准备回复时：
1. 用 LLM 分析主人消息情绪，输出 JSON：{"warmth": -1~1, "effort": 0~1, "attention": 0~1, "topic": "可选", "suppress_hours": 可选}
2. 运行 ${CHIGUO_REPO}/.venv/bin/python ${CHIGUO_REPO}/chiguo_daemon.py --user-msg <消息原文> --analysis '<JSON>'
3. 按 ~/.openclaw/workspace/skills/chiguo/SUN2.md 人格回复
4. 纪念日/假期指令：运行 chiguo_daemon.py --anniversary / --break 对应命令
# CHIGUO-STANDING-ORDER-END
EOSO
    mv "$SO_FILE.tmp" "$SO_FILE"
    say "standing order 已写入 $SO_FILE"
  fi
else
  say "standing order 已存在，跳过写入"
fi

# ── 阶段 3b: 清除 Claude-Code 式 hook / 旧脚本 / 旧 hook ──
if [ "$OLD_HOOK" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    PENDING=1; echo "  [dry-run] 将备份并移除 $CLAUDE_SETTINGS 中的 chiguo hook 条目"
  else
    cp -a "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS.bak"
    "$PY" - "$CLAUDE_SETTINGS" <<'PYJ' || warn "hook 清除失败（.bak 已保留，请手工处理）"
import json, sys
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    cfg = json.load(f)
hooks = cfg.get("hooks", {})
ups = hooks.get("UserPromptSubmit", [])
kept = [e for e in ups if "chiguo" not in json.dumps(e)]
if len(kept) != len(ups):
    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    cfg["hooks"] = hooks
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("hook 条目已移除")
PYJ
  fi
else
  say "无 .claude/settings.json chiguo hook"
fi
if [ -f "$ON_USER_MSG" ]; then
  would "cp -a '$ON_USER_MSG' '$ON_USER_MSG.bak' && rm -f '$ON_USER_MSG'"
fi
if [ -n "$CHIGUO_NATIVE_HOOKS" ]; then
  for h in $CHIGUO_NATIVE_HOOKS; do
    echo "[chiguo-integ] 禁用旧 OpenClaw hook: $h"
    would "openclaw hooks disable $h"
  done
fi
if [ -n "$LEGACY_HANDLERS" ]; then
  echo "[chiguo-integ] legacy hooks.internal.handlers → 官方迁移工具"
  would "openclaw doctor --fix"
fi

# ── 阶段 4: 收尾验证 ──────────────────────────────────────
if [ "$DRY" = 1 ]; then
  [ "$PENDING" = 1 ] && exit 1
  say "dry-run：无待办（全部已安装）"
  exit 0
fi
openclaw config validate >/dev/null 2>&1 && say "config validate OK" || { warn "config validate 失败"; PENDING=1; }
if ! openclaw automations list 2>/dev/null | grep -q chiguo-check; then
  warn "作业 chiguo-check 未在册"; PENDING=1
else
  say "作业 chiguo-check 在册"
fi
if [ "$MODE" = yes ]; then
  echo "[chiguo-integ] 官方审计（危险自动化开关后）..."
  openclaw security audit --deep 2>&1 | tail -5
fi
[ "$PENDING" = 1 ] && exit 1
say "集成安装完成 ✓（端到端冒烟: openclaw automations run chiguo-check --wait --wait-timeout 10m）"
exit 0
