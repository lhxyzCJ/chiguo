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

# ── 2. 可选依赖 lancedb(记忆库,缺省优雅降级 JSON) ────
if uv run python -c "import lancedb" >/dev/null 2>&1; then
    say "lancedb OK → 将读取 \$HOME/.pi-agent/memory/lancedb-pro"
else
    warn "lancedb 未安装 → 记忆降级为 JSON 模式(可运行: uv pip install lancedb)"
fi

# ── 3. 全量自检(35 py + 9 script 测试,任一失败即中止) ──────────
TESTS=(test_chiguo_math test_holiday_parser test_schedule_parser
       test_integration test_monitor test_eventbus test_personality
       test_bayesian test_composer test_ebbinghaus test_longing
       test_escape_valve test_feedback test_trigger test_topics
       test_circadian test_followup test_netease_proof test_netease_service
       test_envcheck test_composer_trade test_personality_init
       test_toml_binding test_adapt_personality test_pi_health
       test_anniversary test_schedule_override test_day_plan test_recall
       test_attention_tiers test_availability test_trigger_scale
       test_isolation test_schedule_plan test_schedule_cli)
say "运行 Node 测试(5 个文件) ..."
for t in test_pi_run test_bridge_askpi test_bridge_cmd test_bridge_health test_bridge_schedule; do
    node "tests/$t.mjs" >/dev/null || fail "$t.mjs 失败,中止部署"
done
say "运行脚本测试(4 个文件) ..."
bash tests/test_install_pi.sh >/dev/null || fail "test_install_pi.sh 失败,中止部署"
bash tests/test_wechat_bridge.sh >/dev/null || fail "test_wechat_bridge.sh 失败,中止部署"
bash tests/test_netease_api.sh >/dev/null || fail "test_netease_api.sh 失败,中止部署"
bash tests/test_tick_health.sh >/dev/null || fail "test_tick_health.sh 失败,中止部署"
bash tests/test_service.sh >/dev/null || fail "test_service.sh 失败,中止部署"
say "运行全量 Python 测试(${#TESTS[@]} 个文件) ..."
for t in "${TESTS[@]}"; do
    uv run python "tests/$t.py" >/dev/null || fail "$t.py 失败,中止部署"
done
say "全部测试通过 ✓"

# ── 4. 环境就绪检查(pi/依赖/数据文件,chiguo_envcheck.py) ──
say "运行环境检查 ..."
set +e
if [[ "$*" == *--skip-pi* ]]; then
    uv run python chiguo_envcheck.py --skip-pi
else
    uv run python chiguo_envcheck.py
fi
EC=$?
set -e
case $EC in
    0) say "环境就绪 ✓" ;;
    1) warn "环境存在警告(见上方 JSON,系统可运行但部分降级)" ;;
    2) fail "环境存在严重问题(见上方 JSON),请先修复再继续(若为 pi 缺失: 请先安装 pi-agent,或 --skip-pi 跳过 pi 环境)" ;;
esac

# ── 5 微信桥 wechat-bridge 安装+自启（可跳过: bash deploy.sh --skip-bridge）──
BRIDGE_OK=0
if [[ "$*" != *--skip-bridge* ]]; then
    say "安装微信桥（wechat-bridge，发送端点 + 回复回传）..."
    set +e
    bash "$PROJECT_DIR/scripts/wechat-bridge.sh" install
    BI=$?
    set -e
    case $BI in
        0) say "微信桥安装完成 ✓" ;;
        2) fail "微信桥安装严重问题，请修复后重试（或 --skip-bridge 跳过）" ;;
    esac
    set +e
    bash "$PROJECT_DIR/scripts/service.sh" autostart
    BC=$?
    set -e
    BRIDGE_OK=0
    [ "$BC" = 0 ] && BRIDGE_OK=1
    case $BC in
        0) say "微信桥 systemd 自启注册并启动 ✓" ;;
        1) warn "微信桥自启注册有警告（service.sh autostart 排查；非 root 机器可改用 bash scripts/service.sh temp）" ;;
        2) warn "微信桥自启注册失败（bash scripts/service.sh status 排查）" ;;
    esac
fi

# ── 5.5 pi 环境安装（可跳过: bash deploy.sh --skip-pi）──────────
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

# ── 5.6 网易云 API 服务（可跳过: bash deploy.sh --skip-netease）──
NETEASE_OK=0
if [[ "$*" != *--skip-netease* ]]; then
    say "安装网易云 API 服务（api-enhanced，可选来源；扫码登录: uv run python -m netease.bridge --login）..."
    set +e
    bash "$PROJECT_DIR/scripts/netease-api.sh" install
    NC=$?
    set -e
    case $NC in
        0) NETEASE_OK=1; say "网易云 API 服务就绪 ✓" ;;
        1) warn "网易云 API 服务未就绪（可选来源，降级不影响运行；bash scripts/netease-api.sh install 排查）" ;;
        2) warn "网易云 API 服务安装失败（可选来源；--skip-netease 跳过）" ;;
    esac
fi

# ── 6. 迁移提示 ─────────────────────────────────────────────
# 6.5 集中认证目录（可迁移：拷贝 ~/.chiguo/auth/ 到新机器即自动接入；
#      微信/网易云登录态跨设备可能失效 → 自动重登兜底；pi key 100% 可用）
if [ -d "$HOME/.chiguo/auth" ]; then
    say "检测到集中认证目录 ~/.chiguo/auth/ → 微信登录态/网易云 cookie/pi key 自动接入"
else
    warn "未检测到 ~/.chiguo/auth/ 集中认证目录 → 登录需手动（bash scripts/wechat-bridge.sh login / uv run python -m netease.bridge --login / install_pi.sh 阶段 5；网易云 API 服务: bash scripts/netease-api.sh install）"
fi
if [ ! -f chiguo_state.json ]; then
    warn "chiguo_state.json 不存在 → 若从旧运行机迁移,请手动拷贝 state/decisions 等运行时文件(不进 git)"
    warn "  (旧机的 chiguo_state.json/chiguo_decisions.jsonl/netease/netease_cookie.txt)"
fi

cat <<EOF

────────────────── 部署完成 ──────────────────
 微信桥:        $( [ "$BRIDGE_OK" = 1 ] && echo "已安装并启动（登录态本地保留不进 git; bash scripts/wechat-bridge.sh status）" || echo "未启动（bash scripts/wechat-bridge.sh install && start 排查）")
 pi 环境:       $( [ "$PI_OK" = 1 ] && echo "已由本脚本自动完成（memory-lancedb-pro 扩展 + crontab + provider key，随 toml [host].provider）" || echo "未安装或未完全安装（bash scripts/install_pi.sh --dry-run 排查）")
 网易云 API:    $( [ "$NETEASE_OK" = 1 ] && echo "已安装并常驻（api-enhanced v4.39.0，systemd: netease-api.service；bash scripts/netease-api.sh status）" || echo "未安装/未就绪（bash scripts/netease-api.sh install 排查；--skip-netease 跳过）")
  手动重跑/排查: bash scripts/install_pi.sh --dry-run（扫描）| --yes（自动修复）
  端到端冒烟:   bash scripts/chiguo-tick.sh（tick 手动触发 → 微信收到）

手动验证:
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py            # 单次决策 → JSON
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py --stats --alerts --monitor
  $PROJECT_DIR/.venv/bin/python chiguo_monitor.py --summary --health
  $PROJECT_DIR/.venv/bin/python chiguo_watchdog.py          # 健康检查,exit 0/1/2
EOF
