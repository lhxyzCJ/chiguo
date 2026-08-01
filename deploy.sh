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

# ── 3. 全量自检(22 py + 3 脚本测试,任一失败即中止) ──────────
TESTS=(test_chiguo_math test_holiday_parser test_integration test_monitor
       test_eventbus test_personality test_bayesian test_composer
       test_ebbinghaus test_longing test_escape_valve test_feedback
       test_trigger test_topics test_circadian test_followup
       test_netease_proof test_netease_service test_envcheck
       test_composer_trade test_personality_init test_toml_binding)
say "运行脚本测试(3 个文件) ..."
node test_trigger_script.js >/dev/null || fail "test_trigger_script.js 失败,中止部署"
bash test_install_integration.sh >/dev/null || fail "test_install_integration.sh 失败,中止部署"
bash test_wechat_bridge.sh >/dev/null || fail "test_wechat_bridge.sh 失败,中止部署"
say "运行全量 Python 测试(${#TESTS[@]} 个文件) ..."
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
    2) fail "环境存在严重问题(见上方 JSON),请先修复再继续(若为 pi 缺失: 请先安装 pi-agent,或 --skip-pi 跳过 pi 环境)" ;;
esac

# ── 5. OpenClaw 集成安装（可跳过: bash deploy.sh --skip-integration）──
INTEG_OK=0
if [[ "$*" != *--skip-integration* ]]; then
    say "安装 OpenClaw 集成（trigger-script 门控 + standing order）..."
    set +e
    bash "$PROJECT_DIR/scripts/install_integration.sh" "$@"
    IC=$?
    set -e
    [ "$IC" = 0 ] && INTEG_OK=1
    case $IC in
        0) say "集成安装完成 ✓" ;;
        1) warn "集成安装有警告/残留未处理（见上方输出），daemon 部署不受影响" ;;
        2) fail "集成安装严重问题，请修复后重试（或 --skip-integration 跳过）" ;;
    esac
fi

# ── 5.5 微信桥 wechat-bridge 安装+启动（可跳过: bash deploy.sh --skip-bridge）──
BRIDGE_OK=0
if [[ "$*" != *--skip-bridge* ]]; then
    say "安装微信桥（wechat-bridge，发送端点 + 回复回传）..."
    set +e
    bash "$PROJECT_DIR/scripts/wechat-bridge.sh" install
    BI=$?
    set -e
    case $BI in
        0) BRIDGE_OK=1; say "微信桥安装完成 ✓" ;;
        2) fail "微信桥安装严重问题，请修复后重试（或 --skip-bridge 跳过）" ;;
    esac
    set +e
    bash "$PROJECT_DIR/scripts/wechat-bridge.sh" start
    BC=$?
    set -e
    BRIDGE_OK=0
    [ "$BC" = 0 ] && BRIDGE_OK=1
    case $BC in
        0) say "微信桥启动 ✓" ;;
        1) warn "微信桥未启动（通常=无登录态/缺 .env；扫码见日志 /tmp/opencode/wechat-bridge.log 或 bash scripts/wechat-bridge.sh status）" ;;
    esac
fi

# ── 5.6 pi 环境安装（可跳过: bash deploy.sh --skip-pi）──────────
PI_OK=0
if [[ "$*" != *--skip-pi* ]]; then
    say "安装 pi 环境（memory-lancedb-pro 扩展 + settings/json5/auth + crontab）..."
    set +e
    bash "$PROJECT_DIR/scripts/install_pi.sh" "$@"
    PC=$?
    set -e
    [ "$PC" = 0 ] && PI_OK=1
    case $PC in
        0) say "pi 环境安装完成 ✓" ;;
        1) warn "pi 环境有警告/残留未处理（见上方输出），消息生成可能受影响" ;;
        2) fail "pi 环境严重问题（pi-agent 未安装?），请先修复后重试（或 --skip-pi 跳过）" ;;
    esac
fi

# ── 6. 迁移提示 ─────────────────────────────────────────────
if [ ! -f chiguo_state.json ]; then
    warn "chiguo_state.json 不存在 → 若从旧运行机迁移,请拷贝 state/decisions 等运行时文件"
    warn "  (旧机的 chiguo_state.json/chiguo_decisions.jsonl/netease_cookie.txt)"
fi

cat <<EOF

────────────────── 部署完成 ──────────────────
OpenClaw 集成: $( [ "$INTEG_OK" = 1 ] && echo "已由本脚本自动完成" || echo "未安装或未完全安装（见上方输出; bash scripts/install_integration.sh --dry-run 排查）")
  手动重跑/排查: bash scripts/install_integration.sh --dry-run（扫描）| --yes（自动修复）
  端到端冒烟:   openclaw cron run chiguo-check --expect-final
  完整指南:     doc/OPENCLAW_INTEGRATION.md
 微信桥:        $( [ "$BRIDGE_OK" = 1 ] && echo "已安装并启动（登录态随仓库保留; bash scripts/wechat-bridge.sh status）" || echo "未启动（bash scripts/wechat-bridge.sh install && start 排查）")
pi 环境:       $( [ "$PI_OK" = 1 ] && echo "已由本脚本自动完成（memory-lancedb-pro 扩展 + crontab + opencode-go）" || echo "未安装或未完全安装（bash scripts/install_pi.sh --dry-run 排查）")
  手动重跑/排查: bash scripts/install_pi.sh --dry-run（扫描）| --yes（自动修复）
  端到端冒烟:   bash scripts/chiguo-tick.sh（tick 手动触发 → 微信收到）

手动验证:
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py            # 单次决策 → JSON
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py --stats --alerts --monitor
  $PROJECT_DIR/.venv/bin/python chiguo_monitor.py --summary --health
  $PROJECT_DIR/.venv/bin/python chiguo_watchdog.py          # 健康检查,exit 0/1/2
EOF
