#!/usr/bin/env bash
# pi-auth — 解析 pi API key 到 OPENCODE_API_KEY（供 source 使用;opencode-go 优先 → toml provider 回退）
# 抽取自 chiguo-tick.sh（cron 环境无 key,memory-lancedb-pro 扩展需要）
# 用法: source scripts/pi-auth.sh  （本脚本按约定假定 REPO 已定义;否则从自身路径推导）
REPO="${CHIGUO_REPO:-${REPO:-$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/..}}"
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
