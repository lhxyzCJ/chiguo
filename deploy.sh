#!/usr/bin/env bash
# ============================================================
# 迟菓主动消息系统 — 目标机器一键部署/自检
# 用法: 在 clone 下来的项目根目录执行  bash deploy.sh
# 假设: 已装 git;仓库为 private(含个人记忆数据);运行时文件均为
#       相对/~/路径解析,可在任意用户的任意目录运行。
# ============================================================
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

say() { printf '\033[1;32m[chiguo]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chiguo]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[chiguo]\033[0m %s\n' "$*"; exit 1; }

# ── 1. Python 3.14 + uv ─────────────────────────────────────
if ! command -v uv >/dev/null 2>&1; then
    say "未找到 uv,正在安装(写入 \$HOME/.local/bin) ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.14 >/dev/null 2>&1 || true
if [ ! -x .venv/bin/python ]; then
    uv venv --python 3.14 >/dev/null
fi
say "Python: $(uv run python --version)($(uv run python -c 'import sys;print(sys.executable)'))"

# ── 2. 可选依赖 lancedb(OpenClaw 记忆,缺省优雅降级 JSON) ────
if uv run python -c "import lancedb" >/dev/null 2>&1; then
    say "lancedb OK → 将读取 \$HOME/.openclaw/memory/lancedb-pro"
else
    warn "lancedb 未安装 → 记忆降级为 JSON 模式(可运行: uv pip install lancedb)"
fi

# ── 3. 全量自检(19 个测试,任一失败即中止) ───────────────────
TESTS=(test_chiguo_math test_holiday_parser test_integration test_monitor
       test_eventbus test_personality test_bayesian test_composer
       test_ebbinghaus test_longing test_escape_valve test_feedback
       test_trigger test_topics test_circadian test_followup
       test_netease_proof test_netease_service test_envcheck)
say "运行全量测试(${#TESTS[@]} 个文件) ..."
for t in "${TESTS[@]}"; do
    uv run python "$t.py" >/dev/null || fail "$t.py 失败,中止部署"
done
say "全部测试通过 ✓"

# ── 4. 环境就绪检查(OpenClaw/依赖/数据文件,chiguo_envcheck.py) ──
say "运行环境检查 ..."
set +e
uv run python chiguo_envcheck.py
EC=$?
set -e
case $EC in
    0) say "环境就绪 ✓" ;;
    1) warn "环境存在警告(见上方 JSON,系统可运行但部分降级)" ;;
    2) fail "环境存在严重问题(见上方 JSON),请先修复再继续" ;;
esac

# ── 6. 迁移提示 ─────────────────────────────────────────────
if [ ! -f chiguo_state.json ]; then
    warn "chiguo_state.json 不存在 → 若从旧运行机迁移,请拷贝 state/decisions 等运行时文件"
    warn "  (旧机的 chiguo_state.json/chiguo_decisions.jsonl/netease_cookie.txt)"
fi

cat <<EOF

────────────────── 部署完成 ──────────────────
定时任务: 通过 OpenClaw cron 注册(每 30 分钟),参考 doc/OPENCLAW_INTEGRATION.md:
  openclaw cron add \
    --name chiguo-check \
    --cron "*/30 * * * *" \
    --session main --wake now \
    --system-event "运行 $PROJECT_DIR/.venv/bin/python $PROJECT_DIR/chiguo_daemon.py。解析 stdout JSON。若 action=idle 回复 NO_REPLY;若 action=send 按 SUN2.md 人格生成消息并发送(见 doc/OPENCLAW_INTEGRATION.md 完整文本)"

手动验证:
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py            # 单次决策 → JSON
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py --stats --alerts --monitor
  $PROJECT_DIR/.venv/bin/python chiguo_monitor.py --summary --health
  $PROJECT_DIR/.venv/bin/python chiguo_watchdog.py          # 健康检查,exit 0/1/2
EOF
