#!/usr/bin/env bash
# replan-tick — 重分析 crontab 入口(与 chiguo-tick 解耦;Python 无法 source bash,必须经此包装)
set -euo pipefail
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
source "$(dirname "$(readlink -f "$0")")/agent-auth.sh"
cd "$REPO"
mkdir -p "$REPO/logs"   # R6:重定向目标目录缺 → cron 整条命令失败(与 chiguo-tick 同规约)
"$REPO/.venv/bin/python" -m schedule.replan --check >> "$REPO/logs/cron-replan.log" 2>&1
