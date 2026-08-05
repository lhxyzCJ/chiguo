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
ssh "$HOST" "tar cf - -C ~/.pi/agent/sessions/--root-chiguo-wechat-bridge-- ." | tar xf - -C "$SESSION_DIR"
echo "    ok: $(ls "$SESSION_DIR" | wc -l) 个文件"

echo "==> 2/5 LanceDB 记忆库(排除锁)"
ssh "$HOST" "tar cf - --exclude='*.lock' -C ~/.pi-agent/memory/lancedb-pro ." | tar xf - -C "$HOME/.pi-agent/memory/lancedb-pro"
echo "    ok: $(du -sh "$HOME/.pi-agent/memory/lancedb-pro" | cut -f1)"

echo "==> 3/5 chiguo_state.json + chiguo_memories.json"
scp -q "$HOST":~/chiguo/chiguo_state.json "$REPO/chiguo_state.json"
scp -q "$HOST":~/chiguo/data/chiguo_memories.json "$REPO/data/chiguo_memories.json"
echo "    ok"

echo "==> 4/5 日志(cron + pi-run 遥测)"
mkdir -p "$REPO/logs"
scp -q "$HOST":~/chiguo/logs/pi-run.log "$REPO/logs/pi-run.log" 2>/dev/null || echo "    (埋埋暂无 pi-run.log)"
scp -q "$HOST":~/chiguo/logs/cron-tick.log "$REPO/logs/cron-tick.log" 2>/dev/null || true
scp -q "$HOST":~/chiguo/logs/cron-replan.log "$REPO/logs/cron-replan.log" 2>/dev/null || true
echo "    ok"

echo "==> 5/5 完成:开发机 = 埋埋快照"
