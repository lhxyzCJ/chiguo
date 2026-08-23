#!/usr/bin/env bash
# scripts/verify-audit.sh , 仓库侧薄封装（代理到 ~/chiguo-meta/audit/scripts/verify-audit.sh）
# 只读校验，不改源码；缺失审计区时给出安装提示而非静默成功
set -euo pipefail

AUDIT_HARNESS="$HOME/chiguo-meta/audit/scripts/verify-audit.sh"

if [ -x "$AUDIT_HARNESS" ]; then
  exec bash "$AUDIT_HARNESS" "$@"
fi

if [ -f "$AUDIT_HARNESS" ]; then
  exec bash "$AUDIT_HARNESS" "$@"
fi

echo "[verify-audit] audit harness not found: $AUDIT_HARNESS" >&2
echo "  expected: ~/chiguo-meta/audit/scripts/verify-audit.sh (wave3 产出)" >&2
echo "  fallback: run 'bash ~/chiguo-meta/audit/scripts/verify-audit.sh' directly" >&2
exit 2
