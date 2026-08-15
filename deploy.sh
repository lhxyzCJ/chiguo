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
    say "未找到 uv,正在安装(固定版本 + SHA256 校验,写入 \$HOME/.local/bin) ..."
    UV_VERSION="0.12.3"
    # uv 发布物: uv-{target}.tar.gz（含 uv/uvx），同目录 *.sha256 内容为 "<hash>  <file>"
    case "$(uname -s)-$(uname -m)" in
        Linux-x86_64)  UV_TARGET="x86_64-unknown-linux-gnu" ;;
        Linux-aarch64) UV_TARGET="aarch64-unknown-linux-gnu" ;;
        Darwin-x86_64) UV_TARGET="x86_64-apple-darwin" ;;
        Darwin-arm64)  UV_TARGET="aarch64-apple-darwin" ;;
        *) fail "不支持的平台 $(uname -s)-$(uname -m),请手动安装 uv(https://docs.astral.sh/uv/)" ;;
    esac
    UV_ARCHIVE="uv-${UV_TARGET}.tar.gz"
    UV_TMP="$(mktemp -d "${TMPDIR:-/tmp}/uv-install-XXXXXX")"
    curl -fsSL --retry 3 -o "$UV_TMP/$UV_ARCHIVE" "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/$UV_ARCHIVE"
    curl -fsSL --retry 3 -o "$UV_TMP/$UV_ARCHIVE.sha256" "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/$UV_ARCHIVE.sha256"
    if command -v sha256sum >/dev/null 2>&1; then
        ( cd "$UV_TMP" && sha256sum -c "$UV_ARCHIVE.sha256" ) || fail "uv 下载校验失败(SHA256 不匹配)"
    else
        ( cd "$UV_TMP" && shasum -a 256 -c "$UV_ARCHIVE.sha256" ) || fail "uv 下载校验失败(SHA256 不匹配)"
    fi
    tar xzf "$UV_TMP/$UV_ARCHIVE" -C "$UV_TMP"
    mkdir -p "$HOME/.local/bin"
    install -m 755 "$UV_TMP/uv-${UV_TARGET}/uv" "$UV_TMP/uv-${UV_TARGET}/uvx" "$HOME/.local/bin/"
    rm -rf "$UV_TMP"
    export PATH="$HOME/.local/bin:$PATH"
fi
uv python install 3.14 >/dev/null 2>&1 || true
if [ ! -x .venv/bin/python ]; then
    say "首次建 venv + 同步依赖（uv sync --all-extras：mem0 记忆 / openpyxl 课表）..."
    uv sync --all-extras || fail "uv sync --all-extras 失败,请检查网络后重试（可先手动: uv sync --all-extras）"
fi
say "Python: $(uv run python --version)($(uv run python -c 'import sys;print(sys.executable)'))"

# ── 2. 必需依赖 mem0(唯一记忆后端;缺失即中止部署) ────
if uv run python -c "import mem0" >/dev/null 2>&1; then
    say "mem0 OK → 记忆库 data/mem0(qdrant 本地 + ollama qwen3-embedding)"
else
    fail "mem0 未安装 → 记忆层缺失(唯一记忆后端,必需);请运行 uv sync --all-extras"
fi

# ── 3. 全量自检(ci-test.sh 单一入口，测试计数由脚本动态扫描磁盘自述；stub 自举) ──
say "运行全量自检(bash scripts/ci-test.sh,任一失败即中止) ..."
bash "$PROJECT_DIR/scripts/ci-test.sh" || fail "全量测试失败,中止部署"
say "全部测试通过 ✓"

# ── 4. 环境就绪检查(agent 后端/依赖/数据文件,chiguo_envcheck.py) ──
say "运行环境检查 ..."
set +e
if [[ "$*" == *--skip-agent* ]]; then
    uv run python chiguo_envcheck.py --skip-agent
else
    uv run python chiguo_envcheck.py
fi
EC=$?
set -e
case $EC in
    0) say "环境就绪 ✓" ;;
    1) warn "环境存在警告(见上方 JSON,系统可运行但部分降级)" ;;
    2) fail "环境存在严重问题(见上方 JSON),请先修复再继续(若为 agent 后端缺失: 请先安装 agent 后端,或 --skip-agent 跳过 agent 后端)" ;;
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

# ── 5.5 agent 后端安装（可跳过: bash deploy.sh --skip-agent）──────────
AGENT_OK=0
if [[ "$*" != *--skip-agent* ]]; then
    say "安装 agent 后端（ollama embedding + auth + crontab + 冒烟）..."
    set +e
    bash "$PROJECT_DIR/scripts/install_agent.sh" "$@"
    PC=$?
    set -e
    [ "$PC" = 0 ] && AGENT_OK=1
    case $PC in
        0) say "agent 后端安装完成 ✓" ;;
        1) warn "agent 后端有警告/残留未处理（见上方输出），消息生成可能受影响" ;;
        2) fail "agent 后端严重问题（未安装?），请先修复后重试（或 --skip-agent 跳过）" ;;
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
#      微信/网易云登录态跨设备可能失效 → 自动重登兜底；agent key 100% 可用）
if [ -d "$HOME/.chiguo/auth" ]; then
    say "检测到集中认证目录 ~/.chiguo/auth/ → 微信登录态/网易云 cookie/agent key 自动接入"
else
    warn "未检测到 ~/.chiguo/auth/ 集中认证目录 → 登录需手动（bash scripts/wechat-bridge.sh login / uv run python -m netease.bridge --login / install_agent.sh 阶段 5；网易云 API 服务: bash scripts/netease-api.sh install）"
fi
if [ ! -f chiguo_state.json ]; then
    warn "chiguo_state.json 不存在 → 若从旧运行机迁移,请手动拷贝 state/decisions 等运行时文件(不进 git)"
    warn "  (旧机的 chiguo_state.json/chiguo_decisions.jsonl/netease/netease_cookie.txt)"
fi

# 6.6 context_token 新鲜度检查（#224 主动发送前置条件）：
#     微信服务端无公开 TTL，实测最后一次收到用户消息后约 35h 失效；收到消息自动刷新。
#     过期症状 = 主动发送报 [send error] prepare failed → 从微信给机器人发一条消息即恢复。
CT_FILE="$HOME/.chiguo/auth/wechat/context_tokens.json"
if [ -f "$CT_FILE" ]; then
    CT_AGE_H=$(( ($(date +%s) - $(stat -c %Y "$CT_FILE")) / 3600 ))
    if [ "$CT_AGE_H" -ge 24 ]; then
        warn "context_token 已 ${CT_AGE_H}h 未刷新（实测约 35h 过期）→ 主动发送将报 prepare failed；部署后从微信给机器人发一条消息即刷新恢复（无需重扫码）"
    else
        say "context_token 新鲜（${CT_AGE_H}h 前刷新，收到用户消息自动续期）"
    fi
else
    warn "无 context_tokens.json（用户尚未发过消息）→ 首次主动发送前先从微信给机器人发一条消息（缺少 context_token 会报 prepare failed）"
fi

cat <<EOF

────────────────── 部署完成 ──────────────────
 微信桥:        $( [ "$BRIDGE_OK" = 1 ] && echo "已安装并启动（登录态本地保留不进 git; bash scripts/wechat-bridge.sh status）" || echo "未启动（bash scripts/wechat-bridge.sh install && start 排查）")
 agent 后端:       $( [ "$AGENT_OK" = 1 ] && echo "已由本脚本自动完成（crontab + provider key，随 toml [host].provider）" || echo "未安装或未完全安装（bash scripts/install_agent.sh --dry-run 排查）")
 网易云 API:    $( [ "$NETEASE_OK" = 1 ] && echo "已安装并常驻（api-enhanced 跟随上游最新 tag，systemd: netease-api.service；bash scripts/netease-api.sh status）" || echo "未安装/未就绪（bash scripts/netease-api.sh install 排查；--skip-netease 跳过）")
  手动重跑/排查: bash scripts/install_agent.sh --dry-run（扫描）| --yes（自动修复）
  端到端冒烟:   bash scripts/chiguo-tick.sh（tick 手动触发 → 微信收到）

手动验证:
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py            # 单次决策 → JSON
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py --stats --alerts --monitor
  $PROJECT_DIR/.venv/bin/python chiguo_daemon.py --monitor
EOF
