#!/usr/bin/env bash
# dev-sync.sh — 埋埋 → 开发机 一键拉取(覆盖式,方案甲)
# 约定:开发机是工作台,埋埋是唯一数据源;开发测试一律用临时 session id + 临时 dbPath,
# 拉取无脑覆盖本地,本地永远等于埋埋快照。
# 用法: bash scripts/dev-sync.sh [host]
set -euo pipefail

HOST="${1:-埋埋}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
SESSION_DIR="$HOME/.pi/agent/sessions/--root-chiguo-wechat-bridge--"

echo "==> 1/5 会话 jsonl(chiguo-main/extract)"
mkdir -p "$SESSION_DIR"
ssh -o BatchMode=yes "$HOST" "tar cf - -C ~/.pi/agent/sessions/--root-chiguo-wechat-bridge-- ." | tar xf - -C "$SESSION_DIR"
echo "    ok: $(ls "$SESSION_DIR" | wc -l) 个文件"

echo "==> 2/5 mem0 记忆库 data/mem0(qdrant,排除锁)"
mkdir -p "$REPO/data/mem0"
ssh -o BatchMode=yes "$HOST" "tar cf - --exclude='*.lock' -C ~/chiguo/data/mem0 ." | tar xf - -C "$REPO/data/mem0"
echo "    ok: $(du -sh "$REPO/data/mem0" | cut -f1)"

echo "==> 3/5 chiguo_state.json"
scp -q -o BatchMode=yes "$HOST":~/chiguo/chiguo_state.json "$REPO/chiguo_state.json"
echo "    ok"

echo "==> 4/5 日志(cron + agent-run 遥测)"
mkdir -p "$REPO/logs"
scp -q -o BatchMode=yes "$HOST":~/chiguo/logs/agent-run.log "$REPO/logs/agent-run.log" 2>/dev/null || echo "    (埋埋暂无 agent-run.log)"
scp -q -o BatchMode=yes "$HOST":~/chiguo/logs/cron-tick.log "$REPO/logs/cron-tick.log" 2>/dev/null || true
scp -q -o BatchMode=yes "$HOST":~/chiguo/logs/cron-replan.log "$REPO/logs/cron-replan.log" 2>/dev/null || true
echo "    ok"

echo "==> 5/5 完成:开发机 = 埋埋快照"
