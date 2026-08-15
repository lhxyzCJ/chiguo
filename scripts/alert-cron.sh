#!/usr/bin/env bash
# alert-cron — 告警 cron 化入口（Q24/#275）：检出并持久化告警，将新增
# critical/warn 告警经微信 bridge /send 推送（复用 `chiguo_daemon.py --alerts-push`）。
# 与 chiguo-tick/replan-tick 解耦，可独立注册进系统 crontab（典型 `0 */2 * * *`）。
set -euo pipefail
# cron 环境 PATH 精简，补齐 python3/常用安装目录（同 chiguo-tick）
export PATH="$PATH:/usr/local/bin:/opt/homebrew/bin"
REPO="${CHIGUO_REPO:-$(dirname "$(readlink -f "$0")")/..}"
# agent-auth.sh 提供 LLM key 环境（告警发送不经 LLM，但保持与 tick 同一环境入口）
source "$(dirname "$(readlink -f "$0")")/agent-auth.sh"
cd "$REPO"
mkdir -p "$REPO/logs"   # R6: 重定向目标目录缺 → cron 整条命令失败（与 chiguo-tick 同规约）
"$REPO/.venv/bin/python" "$REPO/chiguo_daemon.py" --alerts-push >> "$REPO/logs/cron-alert.log" 2>&1
