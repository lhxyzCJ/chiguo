#!/usr/bin/env bash
# install_pi.sh 桩测试：假 pi/curl/crontab + 临时 HOME，验证 dry-run 只读扫描、
# 待办清单与退出码（0=完成/1=有待办/2=严重）、--skip-pi、--yes 写入产物断言、
# auth.json 合并写入与两遍 --yes 幂等（不重复 .bak/crontab 行/扩展条目）、provider 去绑定（15 用例）
set -uo pipefail
TMP="$(mktemp -d /tmp/chiguo-pi-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

ORIG_PATH="$PATH"
mkdir -p "$TMP/bin" "$TMP/nopi" "$TMP/home" "$TMP/repo/scripts"

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

# ── 成功桩 git/npm/node（bin-ok，用例 13/14）：clone 建目录结构 + npm 建 memory-pro CLI ──
mkdir -p "$TMP/bin-ok"
cat > "$TMP/bin-ok/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >> "$CALLS_LOG"
DIR="${@: -1}"
mkdir -p "$DIR/dist/pi-adapter"
touch "$DIR/dist/pi-adapter/index.js"
STUB
cat > "$TMP/bin-ok/npm" <<'STUB'
#!/usr/bin/env bash
echo "npm $*" >> "$CALLS_LOG"
if [ "${1:-}" = install ]; then
  mkdir -p "$PWD/node_modules/.bin"
  printf '#!/usr/bin/env bash\necho "memory-pro stats OK"\n' > "$PWD/node_modules/.bin/memory-pro"
  chmod +x "$PWD/node_modules/.bin/memory-pro"
fi
STUB
cat > "$TMP/bin-ok/node" <<'STUB'
#!/usr/bin/env bash
echo "node $*" >> "$CALLS_LOG"
STUB
for t in git npm node; do chmod +x "$TMP/bin-ok/$t"; done

# ── nopi 目录：无 pi 的隔离工具集（用例 1）──
for t in bash printf command grep awk sed cat cp mkdir mv rm python3 head tail timeout tr dirname; do
  ln -sf "$(command -v "$t")" "$TMP/nopi/$t" 2>/dev/null || true
done

export CALLS_LOG="$TMP/calls.log" CRON_STATE="$TMP/cron_state" TAGS_FILE="$TMP/tags.json"
export CHIGUO_REPO_OVERRIDE="$TMP/repo" HOME="$TMP/home"
# 环境变量隔离：install_pi.sh 的 KEY_VAR 优先 PI_API_KEY 再 OPENCODE_API_KEY，
# 外部（部署机/CI 机）携带任一变量都会改变用例 3/7/12 的行为 → 先清空兜底。
unset PI_API_KEY OPENCODE_API_KEY

# 默认 tags：含 qwen3-embedding（阶段 4 通过；用例 8 覆盖为空）
printf '{"models":[{"name":"qwen3-embedding:0.6b","capabilities":["embedding"]}]}' > "$TAGS_FILE"

WANT="$HOME/.pi-agent/TestForPi-memory-lancedb-pro/dist/pi-adapter/index.js"
JSON5_OK='{"dbPath":"~/.pi-agent/memory/lancedb-pro","embedding":{"provider":"openai-compatible","model":"qwen3-embedding:0.6b","baseURL":"http://localhost:11434/v1"},"autoCapture":true,"autoRecall":true,"smartExtraction":true}'

setup_ready() {  # 预置全部已安装状态（settings/json5/auth/clone/crontab 含 replan-tick 行）
  mkdir -p "$HOME/.pi-agent/TestForPi-memory-lancedb-pro/dist/pi-adapter"
  touch "$HOME/.pi-agent/TestForPi-memory-lancedb-pro/dist/pi-adapter/index.js"
  mkdir -p "$HOME/.pi/agent"
  printf '{"extensions":["%s"]}' "$WANT" > "$HOME/.pi/agent/settings.json"
  printf '%s' "$JSON5_OK" > "$HOME/.pi/agent/memory-lancedb-pro.json5"
  printf '{"opencode-go":{"type":"api_key","key":"sk-test"}}' > "$HOME/.pi/agent/auth.json"
  printf '*/15 * * * * %s/scripts/chiguo-tick.sh >> %s/logs/cron-tick.log 2>&1\n' "$CHIGUO_REPO_OVERRIDE" "$CHIGUO_REPO_OVERRIDE" > "$CRON_STATE"
  printf '*/15 * * * * %s/scripts/replan-tick.sh >> %s/logs/cron-replan.log 2>&1\n' "$CHIGUO_REPO_OVERRIDE" "$CHIGUO_REPO_OVERRIDE" >> "$CRON_STATE"
}
clean_home() { rm -rf "$HOME/.pi" "$HOME/.pi-agent"; rm -f "$CRON_STATE"; }

export PATH="$TMP/bin:$PATH"

# ── 用例 1: 无 pi → 严重，退出 2 ──
clean_home
set +e; PATH="$TMP/nopi" bash scripts/install_pi.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 2 ] && pass "无 pi → 退出 2" || fail "无 pi 期望 2 实得 $RC"

# ── 用例 2: 干净环境 → dry-run 有待办，退出 1 且零写入 ──
clean_home
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_pi.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] && pass "干净环境 dry-run → 退出 1" || fail "期望 1 实得 $RC"
echo "$OUT" | grep -q "git clone" || fail "待办清单缺 clone"
echo "$OUT" | grep -q "chiguo-tick" || fail "待办清单缺 crontab"
echo "$OUT" | grep -q "replan-tick" || fail "待办清单缺 replan crontab"
echo "$OUT" | grep -q "auth.json" || fail "待办清单缺 auth.json"
[ ! -e "$HOME/.pi" ] && [ ! -e "$HOME/.pi-agent" ] || fail "dry-run 不应创建 ~/.pi 或 ~/.pi-agent"
[ ! -f "$CRON_STATE" ] || fail "dry-run 不应注册 crontab"
grep -q "^git " "$CALLS_LOG" && fail "dry-run 不应调用 git" || true
grep -q "^npm " "$CALLS_LOG" && fail "dry-run 不应调用 npm" || true
pass "dry-run 零写入（无 clone/npm/crontab 写入）"

# ── 用例 3: 全部就绪 + 无 OPENCODE_API_KEY → 退出 0 ──
setup_ready
: > "$CALLS_LOG"
set +e; env -u PI_API_KEY -u OPENCODE_API_KEY bash scripts/install_pi.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "全部就绪 dry-run → 退出 0" || fail "期望 0 实得 $RC"
grep -q 'crontab -$' "$CALLS_LOG" && fail "就绪态 dry-run 不应写 crontab" || true

# ── 用例 4: settings.json 指向 Windows 路径 → 待办，且文件不被修改 ──
setup_ready
printf '{"extensions":["/mnt/c/Users/USER/projects/TestForPi-memory-lancedb-pro/dist/pi-adapter/index.js"]}' \
  > "$HOME/.pi/agent/settings.json"
set +e; OUT=$(bash scripts/install_pi.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "Windows 路径期望 1 实得 $RC"
grep -q '/mnt/c/Users/USER' "$HOME/.pi/agent/settings.json" || fail "dry-run 不应改动 settings.json"
[ ! -f "$HOME/.pi/agent/settings.json.bak" ] || fail "dry-run 不应产生 .bak"
pass "Windows 路径残留 → 待办 + settings.json 未被修改"

# ── 用例 5: 全部就绪但 crontab 缺失 → 退出 1 且待办含 chiguo-tick ──
setup_ready
rm -f "$CRON_STATE"
set +e; OUT=$(bash scripts/install_pi.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "crontab 缺失期望 1 实得 $RC"
echo "$OUT" | grep -q "chiguo-tick" || fail "待办未提及 chiguo-tick"
pass "crontab 缺失 → 待办"

# ── 用例 6: 非 TTY 无参数 → 默认 dry-run（不进 ask/read）──
clean_home
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_pi.sh < /dev/null 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "非 TTY 默认 dry-run 期望 1 实得 $RC"
echo "$OUT" | grep -q '\[dry-run\]' || fail "非 TTY 应走 dry-run 路径"
echo "$OUT" | grep -q '\[ask\]' && fail "非 TTY 不应进入 ask 模式" || true
grep -q "^git " "$CALLS_LOG" && fail "默认模式不应 clone" || true
pass "非 TTY 默认 dry-run（无 ask 确认）"

# ── 用例 7: auth.json 缺 opencode-go + OPENCODE_API_KEY 缺失 → 提示环境变量 ──
setup_ready
printf '{"deepseek":{"type":"api_key","key":"old"}}' > "$HOME/.pi/agent/auth.json"
set +e; OUT=$(env -u PI_API_KEY -u OPENCODE_API_KEY bash scripts/install_pi.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "auth 缺 key 期望 1 实得 $RC"
echo "$OUT" | grep -q "OPENCODE_API_KEY" || fail "未提示 OPENCODE_API_KEY 缺失"
pass "auth 缺 opencode-go → 提示 OPENCODE_API_KEY"

# ── 用例 8: ollama tags 缺 qwen3-embedding → 待办 ──
setup_ready
printf '{"models":[]}' > "$TAGS_FILE"
set +e; OUT=$(bash scripts/install_pi.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "ollama 缺模型期望 1 实得 $RC"
echo "$OUT" | grep -q "ollama" || fail "待办未提及 ollama"
printf '{"models":[{"name":"qwen3-embedding:0.6b","capabilities":["embedding"]}]}' > "$TAGS_FILE"
pass "ollama 缺 qwen3-embedding → 待办"

# ── 用例 9: json5 缺 dbPath → 待办 ──
setup_ready
printf '{"embedding":{"model":"qwen3-embedding:0.6b","baseURL":"http://localhost:11434/v1"},"autoCapture":true,"autoRecall":true,"smartExtraction":true}' \
  > "$HOME/.pi/agent/memory-lancedb-pro.json5"
set +e; OUT=$(bash scripts/install_pi.sh --dry-run 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "json5 缺 dbPath 期望 1 实得 $RC"
echo "$OUT" | grep -q "memory-lancedb-pro.json5" || fail "待办未提及 json5"
pass "json5 缺 dbPath → 待办"

# ── 用例 10: 冒烟不在 dry-run 执行（不调 memory-pro / pi -p）──
setup_ready
: > "$CALLS_LOG"
set +e; bash scripts/install_pi.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
grep -q "pi -p" "$CALLS_LOG" && fail "dry-run 不应执行 pi 实调" || true
grep -q "memory-pro" "$CALLS_LOG" && fail "dry-run 不应执行 memory-pro" || true
pass "dry-run 不执行冒烟命令"

# ── 用例 11: --skip-pi → 静默退出 0 ──
clean_home
set +e; OUT=$(bash scripts/install_pi.sh --skip-pi 2>&1); RC=$?; set -e
[ "$RC" = 0 ] && pass "--skip-pi → 退出 0" || fail "期望 0 实得 $RC"
[ -z "$OUT" ] || fail "--skip-pi 应静默无输出"

# ── 用例 12: 干净环境 --yes 模拟失败工具（git 桩 exit 1）→ 阶段 1 失败归 PENDING，退出 1；
#             阶段 2/3/6 仍写入（settings.json/json5/crontab 产物断言），auth 不写（无 key）──
clean_home
set +e; OUT=$(env -u PI_API_KEY -u OPENCODE_API_KEY bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "--yes clone 失败期望 1 实得 $RC"
echo "$OUT" | grep -q "clone/build 失败" || fail "缺少 clone/build 失败警告"
SET_EXT=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["extensions"][0])' "$HOME/.pi/agent/settings.json")
[ "$SET_EXT" = "$WANT" ] || fail "阶段 2 未写入 extensions: $SET_EXT"
grep -q '\.pi-agent/memory/lancedb-pro' "$HOME/.pi/agent/memory-lancedb-pro.json5" || fail "阶段 3 未写入 dbPath"
grep -q 'qwen3-embedding' "$HOME/.pi/agent/memory-lancedb-pro.json5" || fail "阶段 3 未写入 embedding"
grep -q 'chiguo-tick' "$CRON_STATE" || fail "阶段 6 未注册 crontab"
grep -q 'replan-tick' "$CRON_STATE" || fail "阶段 6 未注册 replan crontab"
[ ! -f "$HOME/.pi/agent/auth.json" ] || fail "无 OPENCODE_API_KEY 不应写 auth.json"
pass "--yes 阶段失败 → PENDING + 退出 1（阶段 2/3/6 产物已断言）"

# ── 用例 13: --yes 合并写 auth.json（保留旧 provider 条目 + opencode-go + chmod 600 + .bak 不重复）──
clean_home
mkdir -p "$HOME/.pi/agent"
printf '{"deepseek":{"type":"api_key","key":"sk-old"}}' > "$HOME/.pi/agent/auth.json"
export OPENCODE_API_KEY=sk-new
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "auth 合并写第一遍期望 0 实得 $RC"
AUTH_MERGE=$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")))' "$HOME/.pi/agent/auth.json")
[ "$AUTH_MERGE" = '{"deepseek":{"key":"sk-old","type":"api_key"},"opencode-go":{"key":"sk-new","type":"api_key"}}' ] \
  || fail "auth.json 未合并: $AUTH_MERGE"
[ "$(stat -c %a "$HOME/.pi/agent/auth.json")" = 600 ] || fail "auth.json 权限非 600"
[ -f "$HOME/.pi/agent/auth.json.bak" ] || fail "缺 auth.json.bak"
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "auth 合并写第二遍期望 0 实得 $RC"
[ ! -f "$HOME/.pi/agent/auth.json.bak.bak" ] || fail ".bak 被重复备份"
unset OPENCODE_API_KEY PI_API_KEY
pass "auth.json 合并写入（保留旧条目 + chmod 600）+ .bak 不重复"

# ── 用例 14: --yes 两遍幂等（crontab 单行 / extensions 单条 / 零 .bak / 不重复 clone）──
clean_home
export OPENCODE_API_KEY=sk-ok
: > "$CALLS_LOG"
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "第一遍 --yes 期望 0 实得 $RC"
GIT1=$(grep -c "^git " "$CALLS_LOG" || true)
NPM1=$(grep -c "^npm install" "$CALLS_LOG" || true)
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "第二遍 --yes 期望 0 实得 $RC"
[ "$(grep -c 'chiguo-tick' "$CRON_STATE" || true)" = 1 ] || fail "crontab 重复注册: $(cat "$CRON_STATE")"
[ "$(grep -c 'replan-tick' "$CRON_STATE" || true)" = 1 ] || fail "replan crontab 重复注册: $(cat "$CRON_STATE")"
EXT_N=$(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1]))["extensions"]))' "$HOME/.pi/agent/settings.json")
[ "$EXT_N" = 1 ] || fail "extensions 重复条目: $EXT_N"
[ ! -f "$HOME/.pi/agent/settings.json.bak" ] && [ ! -f "$HOME/.pi/agent/memory-lancedb-pro.json5.bak" ] \
  && [ ! -f "$HOME/.pi/agent/auth.json.bak" ] || fail "两遍 --yes 不应产生 .bak"
[ "$(grep -c "^git " "$CALLS_LOG" || true)" = "$GIT1" ] || fail "第二遍重复 clone"
[ "$(grep -c "^npm install" "$CALLS_LOG" || true)" = "$NPM1" ] || fail "第二遍重复 npm install"
unset OPENCODE_API_KEY PI_API_KEY
pass "--yes 两遍幂等（不重复 crontab/扩展/.bak/clone）"

# ── 用例 15: toml [host].provider=deepseek + PI_API_KEY → 写 deepseek 条目 + 冒烟用 deepseek ──
clean_home
: > "$CALLS_LOG"
printf '[host]\nprovider = "deepseek"\nmodel = "deepseek-chat"\n' > "$CHIGUO_REPO_OVERRIDE/chiguo_proactive.toml"
export PI_API_KEY=sk-ds
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 0 ] || fail "provider=deepseek --yes 期望 0 实得 $RC"
AUTH_DS=$(python3 -c 'import json,sys;print(json.dumps(json.load(open(sys.argv[1])),sort_keys=True,separators=(",",":")))' "$HOME/.pi/agent/auth.json")
[ "$AUTH_DS" = '{"deepseek":{"key":"sk-ds","type":"api_key"}}' ] || fail "auth.json 未写 deepseek 条目: $AUTH_DS"
grep -q "pi -p --provider deepseek" "$CALLS_LOG" || fail "冒烟未用 --provider deepseek: $(cat "$CALLS_LOG")"
grep -q "pi -p --provider opencode-go" "$CALLS_LOG" && fail "冒烟不应再写死 opencode-go" || true
unset PI_API_KEY
rm -f "$CHIGUO_REPO_OVERRIDE/chiguo_proactive.toml"
pass "toml provider=deepseek + PI_API_KEY → deepseek 条目 + 冒烟 --provider deepseek"

# ── 用例 16: 集中认证目录 ~/.chiguo/auth/pi-auth.json → auth.json 导入（目标已有则不动）──
clean_home
mkdir -p "$TMP/home/.chiguo/auth"
printf '{"opencode-go":{"type":"api_key","key":"sk-migrated"}}' > "$TMP/home/.chiguo/auth/pi-auth.json"
set +e; OUT=$(PATH="$TMP/bin-ok:$TMP/bin:$PATH" env -u OPENCODE_API_KEY -u PI_API_KEY bash scripts/install_pi.sh --yes 2>&1); RC=$?; set -e
[ -f "$TMP/home/.pi/agent/auth.json" ] || fail "集中认证导入未生成 auth.json"
grep -q "sk-migrated" "$TMP/home/.pi/agent/auth.json" || fail "导入的 key 不对: $(cat "$TMP/home/.pi/agent/auth.json")"
pass "集中认证目录 pi-auth.json → auth.json 导入"

echo "test_install_pi: 通过"
