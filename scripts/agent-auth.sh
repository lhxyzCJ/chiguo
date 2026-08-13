#!/usr/bin/env bash
# agent-auth — 解析 pi API key 到 OPENCODE_API_KEY（供 source 使用;opencode-go 优先 → toml provider 回退）
# 抽取自 chiguo-tick.sh（cron 环境无 key，pi 生成需要 LLM key）
# 用法: source scripts/agent-auth.sh  （本脚本按约定假定 REPO 已定义;否则从自身路径推导）
REPO="${CHIGUO_REPO:-${REPO:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/..}}"
if [ -z "${OPENCODE_API_KEY:-}" ] && [ -f "$HOME/.pi/agent/auth.json" ]; then
  TICK_FALLBACK_PROVIDER="$(sed -n 's/^[[:space:]]*provider *= *"\([^"]*\)".*/\1/p' "$REPO/chiguo_proactive.toml" | head -1 || true)"
  [ -n "$TICK_FALLBACK_PROVIDER" ] || TICK_FALLBACK_PROVIDER=opencode-go
  # H-1: 用仓库 venv python（含真实 PATH 下的解释器），避免 cron 精简 PATH 下
  # 裸 python3 缺失 → key 静默为空；venv 缺失时显式告警（不再 2>/dev/null 完全静默）。
  VENV_PY="$REPO/.venv/bin/python"
  if [ ! -x "$VENV_PY" ]; then
    echo "[agent-auth] 警告: $VENV_PY 不存在,无法读取 LLM key(cron 环境需 venv)" >&2
    OPENCODE_API_KEY=""
  else
    OPENCODE_API_KEY="$(TICK_FALLBACK_PROVIDER="$TICK_FALLBACK_PROVIDER" "$VENV_PY" -c "
import json,os
try:
    d=json.load(open(os.path.expanduser('~/.pi/agent/auth.json')))
    key = (d.get('opencode-go') or {}).get('key') or (d.get(os.environ.get('TICK_FALLBACK_PROVIDER','opencode-go')) or {}).get('key','')
    print(key or '')
except Exception: print('')
" 2>/dev/null || true)"
  fi
  export OPENCODE_API_KEY
fi
