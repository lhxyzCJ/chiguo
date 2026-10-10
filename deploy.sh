#!/usr/bin/env bash
# ============================================================
# 迟菓 — 目标机器一键部署/自检（V2 runtime + 微信被动桥 + pi-agent 生成）
# 用法: 在项目根目录执行  bash deploy.sh [--skip-bridge] [--skip-agent]
# 假设: 已装 git、node；仓库为 private；运行时文件均为相对/~/路径解析。
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
    say "首次建 venv + 同步依赖（uv sync；纯标准库，无第三方运行依赖）..."
    uv sync || fail "uv sync 失败,请检查网络后重试（可先手动: uv sync）"
fi
say "Python: $(uv run python --version)($(uv run python -c 'import sys;print(sys.executable)'))"

# ── 2. agent 认证（pi provider key → ~/.pi/agent/auth.json；可跳过: --skip-agent）──
AGENT_OK=0
if [[ "$*" != *--skip-agent* ]]; then
    say "配置 agent 认证（provider 读 toml [host].provider；key 从环境变量读，不落盘明文）..."
    if ! command -v pi >/dev/null 2>&1; then
        warn "未检测到 pi → 消息生成端缺失；请先安装 pi-agent 本体后重跑（本脚本只配置不安装）"
    else
        say "pi $(pi --version 2>&1 | head -1)"
        PROVIDER="$(sed -n 's/^provider *= *"\([^"]*\)".*/\1/p' "$PROJECT_DIR/chiguo_proactive.toml" | head -1 || true)"
        [ -n "$PROVIDER" ] || PROVIDER=opencode-go
        AUTH="$HOME/.pi/agent/auth.json"
        # 集中认证迁移源：~/.chiguo/auth/agent-auth.json → ~/.pi/agent/auth.json（目标已有则不动）
        if [ ! -f "$AUTH" ] && [ -f "$HOME/.chiguo/auth/agent-auth.json" ]; then
            mkdir -p "$(dirname "$AUTH")"
            cp -a "$HOME/.chiguo/auth/agent-auth.json" "$AUTH" && chmod 600 "$AUTH" \
                && say "已从 ~/.chiguo/auth/agent-auth.json 导入认证（集中认证目录迁移）"
        fi
        PY="$PROJECT_DIR/.venv/bin/python"
        # auth.json 含 provider 且有真值 key（裸 grep provider 名会把注释/残缺条目误判为已配置）
        auth_has_key() {
            [ -f "$AUTH" ] || return 1
            AUTH_PROVIDER="$PROVIDER" "$PY" - "$AUTH" <<'PYC' >/dev/null 2>&1
import json, os, sys
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
entry = cfg.get(os.environ.get("AUTH_PROVIDER", "opencode-go"))
sys.exit(0 if isinstance(entry, dict) and entry.get("key") else 1)
PYC
        }
        if auth_has_key; then
            say "auth.json OK（已含 $PROVIDER key）"
            AGENT_OK=1
        else
            # key 来源：AGENT_API_KEY（通用名）优先，OPENCODE_API_KEY 兼容回退
            KEY_VAR=AGENT_API_KEY; KEY_VAL="${AGENT_API_KEY:-}"
            [ -n "$KEY_VAL" ] || { KEY_VAR=OPENCODE_API_KEY; KEY_VAL="${OPENCODE_API_KEY:-}"; }
            if [ -z "$KEY_VAL" ]; then
                warn "auth.json 缺 $PROVIDER 且未设置 AGENT_API_KEY/OPENCODE_API_KEY → 无法写入 key；export $KEY_VAR=... 后重跑"
            else
                if [ -f "$AUTH" ]; then cp -a "$AUTH" "$AUTH.bak"; fi
                # key 经环境变量传给 python（argv 会被 ps 看到，明文泄露面更大）
                if KEY_VAL="$KEY_VAL" AUTH_PROVIDER="$PROVIDER" "$PY" - "$AUTH" <<'PYJ'; then
import json, os, sys
p = sys.argv[1]
key = os.environ["KEY_VAL"]
provider = os.environ.get("AUTH_PROVIDER", "opencode-go")
cfg = {}
if os.path.exists(p):
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
os.makedirs(os.path.dirname(p), exist_ok=True)
cfg[provider] = {"type": "api_key", "key": key}
with open(p, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(p, 0o600)
PYJ
                    say "auth.json 已写入 $PROVIDER 条目"
                    AGENT_OK=1
                else
                    warn "auth.json 写入失败（.bak 已保留，请手工处理）"
                fi
            fi
        fi
    fi
fi

# ── 3. 微信桥（被动消息回复 + 发送端点；可跳过: --skip-bridge）──
BRIDGE_OK=0
if [[ "$*" != *--skip-bridge* ]]; then
    say "安装微信桥（wechat-bridge）..."
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
    [ "$BC" = 0 ] && BRIDGE_OK=1
    case $BC in
        0) say "微信桥 systemd 自启注册并启动 ✓" ;;
        1) warn "微信桥自启注册有警告（service.sh autostart 排查；非 root 机器可改用 bash scripts/service.sh temp）" ;;
        2) warn "微信桥自启注册失败（bash scripts/service.sh status 排查）" ;;
    esac
fi

cat <<EOF

────────────────── 部署完成 ──────────────────
  微信桥:     $( [ "$BRIDGE_OK" = 1 ] && echo "已安装并启动（登录态本地保留不进 git; bash scripts/wechat-bridge.sh status）" || echo "未启动（bash scripts/wechat-bridge.sh install && bash scripts/wechat-bridge.sh start 排查）")
  agent 后端: $( [ "$AGENT_OK" = 1 ] && echo "认证就绪（~/.pi/agent/auth.json；provider 见 toml [host].provider）" || echo "未就绪（export AGENT_API_KEY=... 后重跑；或 bash deploy.sh --skip-bridge）")

  手动验证:
  bash scripts/wechat-bridge.sh status          # 桥状态/登录态/context_token 新鲜度
  tail -f /tmp/opencode/wechat-bridge.log       # 桥日志（systemd 模式: journalctl -u chiguo-bridge）
  .venv/bin/python -m app.cli db status         # V2 runtime DB 状态（未初始化时 initialized=false 正常）
EOF
