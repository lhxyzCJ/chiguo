#!/usr/bin/env bash
# install_agent.sh 桩测试：假 pi/curl/crontab + 临时 HOME，验证 dry-run 只读扫描、
# 待办清单与退出码（0=完成/1=有待办/2=严重）、--skip-agent、--yes 写入产物断言、
# auth.json 合并写入与两遍 --yes 幂等（不重复 .bak/crontab）、provider 去绑定（13 用例）
set -uo pipefail
TMP="$(mktemp -d /tmp/chiguo-agent-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

ORIG_PATH="$PATH"
mkdir -p "$TMP/bin" "$TMP/noagent" "$TMP/home" "$TMP/repo/scripts"

# ── 假 pi：--version 有版本；-p 冒烟输出 message_end；调用落 calls.log ──
cat > "$TMP/bin/pi" <<'STUB'
#!/usr/bin/env bash
echo "pi $*" >> "$CALLS_LOG"
case "$1" in
  --version) echo "0.83.0-test" ;;
  -p) echo '{"type":"message_end","text":"ok"}' ;;
  *) echo "unknown" ;;
esac
STUB
chmod +x "$TMP/bin/pi"

# ── 假 curl：/api/tags 输出 $TAGS_FILE；其余失败 ──
cat > "$TMP/bin/curl" <<'STUB'
#!/usr/bin/env bash
echo "curl $*" >> "$CALLS_LOG"
if printf '%s' "$*" | grep -q "/api/tags"; then
  cat "${TAGS_FILE:-/dev/null}" 2>/dev/null || true
else
  echo "curl: unsupported" >&2; exit 1
fi
STUB
chmod +x "$TMP/bin/curl"

# ── 假 crontab：-l 读 $CRON_STATE；- 写入 $CRON_STATE ──
cat > "$TMP/bin/crontab" <<'STUB'
#!/usr/bin/env bash
echo "crontab $*" >> "$CALLS_LOG"
case "${1:-}" in
  -l) [ -f "$CRON_STATE" ] && cat "$CRON_STATE" ;;
  -) cat > "$CRON_STATE" ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$TMP/bin/crontab"

# ── 假 git/npm/node：dry-run 绝不调用（被调用即失败标记）──
for t in git npm node; do
  cat > "$TMP/bin/$t" <<STUB
#!/usr/bin/env bash
echo "$t \$*" >> "\$CALLS_LOG"
exit 1
STUB
  chmod +x "$TMP/bin/$t"
done

# ── 成功桩 git/npm/node（bin-ok，用例 13/14/15/16）：clone 建目录结构 ──
mkdir -p "$TMP/bin-ok"
cat > "$TMP/bin-ok/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS_LOG"
STUB
cat > "$TMP/bin-ok/npm" <<'STUB'
#!/usr/bin/env bash
echo "npm $*" >> "$CALLS_LOG"
STUB
cat > "$TMP/bin-ok/node" <<'STUB'
#!/usr/bin/env bash
echo "node $*" >> "$CALLS_LOG"
STUB
for t in git npm node; do chmod +x "$TMP/bin-ok/$t"; done

# ── 假 systemctl（用例 15/16）：记录调用不落真实 systemd；CHIGUO_SYSTEMCTL 注入 ──
cat > "$TMP/bin-ok/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$CALLS_LOG"
exit 0
STUB
chmod +x "$TMP/bin-ok/systemctl"
export CHIGUO_SYSTEMD_DIR="$TMP/systemd"
export CHIGUO_SYSTEMCTL="$TMP/bin-ok/systemctl"

# ── noagent 目录：无 agent 的隔离工具集（用例 1）──
for t in bash printf command grep awk sed cat cp mkdir mv rm python3 head tail timeout tr dirname; do
  ln -sf "$(command -v "$t")" "$TMP/noagent/$t" 2>/dev/null || true
done

export CALLS_LOG="$TMP/calls.log" CRON_STATE="$TMP/cron_state" TAGS_FILE="$TMP/tags.json"
export CHIGUO_REPO_OVERRIDE="$TMP/repo" HOME="$TMP/home"
# 环境变量隔离：install_agent.sh 的 KEY_VAR 优先 AGENT_API_KEY 再 OPENCODE_API_KEY，
# 外部（部署机/CI 机）携带任一变量都会改变用例 3/7/12 的行为 → 先清空兜底。
unset AGENT_API_KEY OPENCODE_API_KEY

# 默认 tags：含 qwen3-embedding（阶段 4 通过；用例 8 覆盖为空）
printf '{"models":[{"name":"qwen3-embedding:0.6b","capabilities":["embedding"]}]}' > "$TAGS_FILE"

setup_ready() {  # 预置全部已安装状态（auth/crontab 含 replan-tick 行）
  mkdir -p "$HOME/.pi/agent"
  printf '{"opencode-go":{"type":"api_key","key":"sk-test"}}' > "$HOME/.pi/agent/auth.json"
  printf '*/15 * * * * %s/scripts/chiguo-tick.sh >> %s/logs/cron-tick.log 2>&1\n' "$CHIGUO_REPO_OVERRIDE" "$CHIGUO_REPO_OVERRIDE" > "$CRON_STATE"
  printf '*/15 * * * * %s/scripts/replan-tick.sh >> %s/logs/cron-replan.log 2>&1\n' "$CHIGUO_REPO_OVERRIDE" "$CHIGUO_REPO_OVERRIDE" >> "$CRON_STATE"
}
clean_home() { rm -rf "$HOME/.pi"; rm -f "$CRON_STATE"; }

export PATH="$TMP/bin:$PATH"

# ── 用例 1: 无 pi → 严重，退出 2 ──
clean_home
set +e; PATH="$TMP/noagent" bash scripts/install_agent.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 2 ] && pass "无 pi → 退出 2" || fail "无 pi 期望 2 实得 $RC"

# ── 用例 2: 干净环境 → dry-run 有待办，退出 1 且零写入 ──
clean_home
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_agent.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] && pass "干净环境 dry-run → 退出 1" || fail "期望 1 实得 $RC"
echo "$OUT" | grep -q "chiguo-tick" || fail "待办清单缺 crontab"
echo "$OUT" | grep -q "replan-tick" || fail "待办清单缺 replan crontab"
echo "$OUT" | grep -q "auth.json" || fail "待办清单缺 auth.json"
[ ! -e "$HOME/.pi" ] || fail "dry-run 不应创建 ~/.pi"
[ ! -f "$CRON_STATE" ] || fail "dry-run 不应注册 crontab"
grep -q "^git " "$CALLS_LOG" && fail "dry-run 不应调用 git" || true
grep -q "^npm " "$CALLS_LOG" && fail "dry-run 不应调用 npm" || true
pass "dry-run 零写入（无 crontab 写入）"

# ── 用例 3: 全部就绪 + 无 OPENCODE_API_KEY → 退出 0 ──
setup_ready
: > "$CALLS_LOG"
set +e; env -u AGENT_API_KEY -u OPENCODE_API_KEY bash scripts/install_agent.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "全部就绪 dry-run → 退出 0" || fail "期望 0 实得 $RC"
grep -q 'crontab -$' "$CALLS_LOG" && fail "就绪态 dry-run 不应写 crontab" || true

# ── 用例 4: 全部就绪但 crontab 缺失 → 退出 1 且待办含 chiguo-tick ──
setup_ready
rm -f "$CRON_STATE"
set +e; OUT=$(bash scripts/install_agent.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "crontab 缺失期望 1 实得 $RC"
echo "$OUT" | grep -q "chiguo-tick" || fail "待办未提及 chiguo-tick"
pass "crontab 缺失 → 待办"

# ── 用例 6: 非 TTY 无参数 → 默认 dry-run（不进 ask/read）──
clean_home
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_agent.sh < /dev/null 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "非 TTY 默认 dry-run 期望 1 实得 $RC"
echo "$OUT" | grep -q '\[dry-run\]' || fail "非 TTY 应走 dry-run 路径"
echo "$OUT" | grep -q '\[ask\]' && fail "非 TTY 不应进入 ask 模式" || true
grep -q "^git " "$CALLS_LOG" && fail "默认模式不应 clone" || true
pass "非 TTY 默认 dry-run（无 ask 确认）"

# ── 用例 7: auth.json 缺 opencode-go + OPENCODE_API_KEY 缺失 → 提示环境变量 ──
setup_ready
printf '{"deepseek":{"type":"api_key","key":"old"}}' > "$HOME/.pi/agent/auth.json"
set +e; OUT=$(env -u AGENT_API_KEY -u OPENCODE_API_KEY bash scripts/install_agent.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "auth 缺 key 期望 1 实得 $RC"
echo "$OUT" | grep -q "OPENCODE_API_KEY" || fail "未提示 OPENCODE_API_KEY 缺失"
pass "auth 缺 opencode-go → 提示 OPENCODE_API_KEY"

# ── 用例 8: ollama tags 缺 qwen3-embedding → 待办 ──
setup_ready
printf '{"models":[]}' > "$TAGS_FILE"
set +e; OUT=$(bash scripts/install_agent.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "ollama 缺模型期望 1 实得 $RC"
echo "$OUT" | grep -q "ollama" || fail "待办未提及 ollama"
printf '{"models":[{"name":"qwen3-embedding:0.6b","capabilities":["embedding"]}]}' > "$TAGS_FILE"
pass "ollama 缺 qwen3-embedding → 待办"

# ── 用例 9: 冒烟不在 dry-run 执行（不调 pi -p）──
setup_ready
: > "$CALLS_LOG"
set +e; bash scripts/install_agent.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
grep -q "pi -p" "$CALLS_LOG" && fail "dry-run 不应执行 pi 实调" || true
pass "dry-run 不执行冒烟命令"

# ── 用例 11: --skip-agent → 静默退出 0 ──
clean_home
set +e; OUT=$(bash scripts/install_agent.sh --skip-agent 2>&1); RC=$?; set -e
[ "$RC" = 0 ] && pass "--skip-agent → 退出 0" || fail "期望 0 实得 $RC"
[ -z "$OUT" ] || fail "--skip-agent 应静默无输出"

# ── 用例 12: 干净环境 --yes（无 key）→ 阶段 5 PENDING，退出 1；阶段 6 crontab 写入 ──
clean_home
set +e; OUT=$(env -u AGENT_API_KEY -u OPENCODE_API_KEY bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "--yes 无 key 期望 1 实得 $RC"
grep -q 'chiguo-tick' "$CRON_STATE" || fail "阶段 6 未注册 crontab"
grep -q 'replan-tick' "$CRON_STATE" || fail "阶段 6 未注册 replan crontab"
[ ! -f "$HOME/.pi/agent/auth.json" ] || fail "无 OPENCODE_API_KEY 不应写 auth.json"
pass "--yes 无 key → PENDING + 退出 1（阶段 6 crontab 产物已断言）"

# ── 用例 13: --yes 合并写 auth.json（保留旧 provider 条目 + opencode-go + chmod 600 + .bak 不重复）──
clean_home
mkdir -p "$HOME/.pi/agent"
printf '{"deepseek":{"type":"api_key","key":"sk-old"}}' > "$HOME/.pi/agent/auth.json"
export OPENCODE_API_KEY=sk-new
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "auth 合并写第一遍期望 0 实得 $RC"
AUTH_MERGE=$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")))' "$HOME/.pi/agent/auth.json")
[ "$AUTH_MERGE" = '{"deepseek":{"key":"sk-old","type":"api_key"},"opencode-go":{"key":"sk-new","type":"api_key"}}' ] \
  || fail "auth.json 未合并: $AUTH_MERGE"
[ "$(stat -c %a "$HOME/.pi/agent/auth.json")" = 600 ] || fail "auth.json 权限非 600"
[ -f "$HOME/.pi/agent/auth.json.bak" ] || fail "缺 auth.json.bak"
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "auth 合并写第二遍期望 0 实得 $RC"
[ ! -f "$HOME/.pi/agent/auth.json.bak.bak" ] || fail ".bak 被重复备份"
unset OPENCODE_API_KEY AGENT_API_KEY
pass "auth.json 合并写入（保留旧条目 + chmod 600）+ .bak 不重复"

# ── 用例 15: CHIGUO_DAEMON_LOOP=1 → 跳过 tick 注册 + 移除旧 tick 条目（防双发）──
setup_ready
set +e; OUT=$(CHIGUO_DAEMON_LOOP=1 PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "loop 模式期望 0 实得 $RC"
grep -q 'chiguo-tick' "$CRON_STATE" && fail "loop 模式不应注册/保留 chiguo-tick" || true
grep -q 'replan-tick' "$CRON_STATE" || fail "loop 模式应保留 replan-tick"
echo "$OUT" | grep -q "chiguo-tick crontab" || fail "loop 模式应有移除提示"
grep -q -- "--loop 900" "$TMP/systemd/chiguo-daemon.service" \
  || fail "loop unit 应写入 CHIGUO_SYSTEMD_DIR 且含 --loop 900: $(cat "$TMP/systemd/chiguo-daemon.service" 2>/dev/null)"
# R17: loop daemon unit 必须加载 wechat-bridge/.env（_loop_send 读 WECHAT_BRIDGE_TOKEN 防 403 共享鉴权）
grep -q "EnvironmentFile=-" "$TMP/systemd/chiguo-daemon.service" \
  || fail "loop unit 应含 EnvironmentFile=-（R17 共享 token）: $(cat "$TMP/systemd/chiguo-daemon.service" 2>/dev/null)"
grep -q "wechat-bridge/.env" "$TMP/systemd/chiguo-daemon.service" \
  || fail "loop unit EnvironmentFile 应指向 wechat-bridge/.env: $(cat "$TMP/systemd/chiguo-daemon.service" 2>/dev/null)"
pass "CHIGUO_DAEMON_LOOP=1 → 移除 tick 条目、保留 replan（防双发）+ R17 EnvironmentFile"

# ── 用例 16: loop 模式 dry-run → 待办含 daemon unit 提示且零写入 ──
clean_home
set +e; OUT=$(CHIGUO_DAEMON_LOOP=1 bash scripts/install_agent.sh --dry-run 2>&1); RC=$?; set -e
echo "$OUT" | grep -q "chiguo-daemon.service" || fail "loop dry-run 应提示 daemon unit: $(echo "$OUT" | head -5)"
[ ! -f "$CRON_STATE" ] || fail "loop dry-run 不应写 crontab"
pass "loop 模式 dry-run → 待办含 chiguo-daemon.service 且零写入"

# ── 用例 14: --yes 两遍幂等（crontab 单行 / 零 .bak）──
clean_home
export OPENCODE_API_KEY=sk-ok
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "第一遍 --yes 期望 0 实得 $RC"
set +e; OUT=$(bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "第二遍 --yes 期望 0 实得 $RC"
[ "$(grep -c 'chiguo-tick' "$CRON_STATE" || true)" = 1 ] || fail "crontab 重复注册: $(cat "$CRON_STATE")"
[ "$(grep -c 'replan-tick' "$CRON_STATE" || true)" = 1 ] || fail "replan crontab 重复注册: $(cat "$CRON_STATE")"
[ ! -f "$HOME/.pi/agent/auth.json.bak.bak" ] || fail "两遍 --yes 不应重复 .bak"
unset OPENCODE_API_KEY AGENT_API_KEY
pass "--yes 两遍幂等（不重复 crontab/.bak）"

# ── 用例 15: toml [host].provider=deepseek + AGENT_API_KEY → 写 deepseek 条目 + 冒烟用 deepseek ──
clean_home
: > "$CALLS_LOG"
printf '[host]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n' > "$CHIGUO_REPO_OVERRIDE/chiguo_proactive.toml"
export AGENT_API_KEY=sk-ds
set +e; OUT=$(bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "provider=deepseek --yes 期望 0 实得 $RC"
AUTH_DS=$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")))' "$HOME/.pi/agent/auth.json")
[ "$AUTH_DS" = '{"deepseek":{"key":"sk-ds","type":"api_key"}}' ] || fail "auth.json 未写 deepseek 条目: $AUTH_DS"
grep -q "pi -p --provider deepseek" "$CALLS_LOG" || fail "冒烟未用 --provider deepseek: $(cat "$CALLS_LOG")"
grep -q "pi -p --provider opencode-go" "$CALLS_LOG" && fail "冒烟不应再写死 opencode-go" || true
unset AGENT_API_KEY
rm -f "$CHIGUO_REPO_OVERRIDE/chiguo_proactive.toml"
pass "toml provider=deepseek + AGENT_API_KEY → deepseek 条目 + 冒烟 --provider deepseek"

# ── 用例 16: 集中认证目录 ~/.chiguo/auth/agent-auth.json → auth.json 导入（目标已有则不动）──
clean_home
mkdir -p "$TMP/home/.chiguo/auth"
printf '{"opencode-go":{"type":"api_key","key":"sk-migrated"}}' > "$TMP/home/.chiguo/auth/agent-auth.json"
set +e; OUT=$(env -u OPENCODE_API_KEY -u AGENT_API_KEY bash scripts/install_agent.sh --yes 2>&1); RC=$?; set -e
[ -f "$TMP/home/.pi/agent/auth.json" ] || fail "集中认证导入未生成 auth.json"
grep -q "sk-migrated" "$TMP/home/.pi/agent/auth.json" || fail "导入的 key 不对: $(cat "$TMP/home/.pi/agent/auth.json")"
pass "集中认证目录 agent-auth.json → auth.json 导入"

echo "test_install_agent: 通过"
