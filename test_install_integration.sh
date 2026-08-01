#!/usr/bin/env bash
# install_integration.sh 桩测试：假 openclaw + 临时 HOME，验证各阶段行为与退出码
set -euo pipefail
TMP="$(mktemp -d /tmp/chiguo-install-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

# ── 假 openclaw：有状态（JOB_STATE）+ 调用记录 calls.log；行为由 FAKE_* 控制 ──
mkdir -p "$TMP/bin" "$TMP/home" "$TMP/repo/.claude"
cat > "$TMP/bin/openclaw" <<'STUB'
#!/usr/bin/env bash
echo "$0 $*" >> "$CALLS_LOG"
case "$1" in
  -V) echo "v2026.8.1-test" ;;
  automations)
    case "$2" in
      add)
        if echo "$*" | grep -q -- "--help"; then echo "--trigger-script"
        elif [ "${FAKE_ADD_FAIL:-0}" = 1 ]; then echo "error"; exit 1
        else echo "job created"; touch "$JOB_STATE"; fi ;;
      rm) echo "removed"; rm -f "$JOB_STATE" ;;
      get) [ -f "$JOB_STATE" ] && echo "{\"name\":\"chiguo-check\",\"enabled\":true}" || exit 1 ;;
      list)
        if [ -f "$JOB_STATE" ]; then echo "chiguo-check";
        elif [ "${FAKE_HAS_OLD_JOB:-0}" = 1 ]; then echo "chiguo-check-old"; fi ;;
      run) echo "run ok" ;;
    esac ;;
  config)
    case "$2" in
      get) case "$3" in
             cron.triggers.enabled) echo "${FAKE_TRIGGERS_ENABLED:-false}" ;;
             hooks.internal.handlers) echo "${FAKE_LEGACY_HANDLERS:-}" ;;
             *) echo "" ;;
           esac ;;
      set) echo "set ok" ;;
      validate) echo "config valid" ;;
    esac ;;
  gateway) echo "gateway running" ;;
  hooks)
    case "$2" in
      list) echo "${FAKE_NATIVE_HOOKS:-}" ;;
      disable) echo "disabled" ;;
    esac ;;
  doctor) echo "doctor done" ;;
  security) echo "audit ok" ;;
  status) echo "status ok" ;;
  *) echo "unknown" ;;
esac
STUB
chmod +x "$TMP/bin/openclaw"

export CALLS_LOG="$TMP/calls.log" JOB_STATE="$TMP/job_state"
export PATH="$TMP/bin:$PATH" HOME="$TMP/home"
export CHIGUO_REPO_OVERRIDE="$TMP/repo"   # 安装器读此环境变量指向测试仓库根

# ── 用例 1: 无 openclaw → 跳过集成，退出 0 ──
rm -f "$JOB_STATE"
set +e; PATH="/usr/bin:/bin" bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "无 openclaw → 退出 0" || fail "无 openclaw 期望 0 实得 $RC"

# ── 用例 2: 桩不支持 --trigger-script → 警告 + 退出 1 ──
sed -i 's/echo "--trigger-script"/echo "--no-trigger-script"/' "$TMP/bin/openclaw"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] && pass "不支持 trigger-script → 退出 1" || fail "期望 1 实得 $RC"
sed -i 's/echo "--no-trigger-script"/echo "--trigger-script"/' "$TMP/bin/openclaw"

# ── 用例 3: 支持 + 干净环境 → 完成全部阶段，退出 0 ──
rm -f "$JOB_STATE" "$HOME/.openclaw/workspace/agents/main/AGENTS.md" "$TMP/repo/.claude/settings.json"
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "支持 + 干净环境 → 退出 0" || fail "期望 0 实得 $RC"
grep -q "automations add --name chiguo-check" "$CALLS_LOG" || fail "未注册新作业"
grep -q "config set cron.triggers.enabled true" "$CALLS_LOG" || fail "未执行 config set"
grep -q "security audit --deep" "$CALLS_LOG" || fail "未执行 security audit --deep"

# ── 用例 4: standing order 写入且幂等 ──
SO="$HOME/.openclaw/workspace/agents/main/AGENTS.md"
[ -f "$SO" ] || fail "standing order 未写入 $SO"
grep -q "CHIGUO-STANDING-ORDER-START" "$SO" || fail "缺少起始标记"
grep -q -- "--user-msg" "$SO" || fail "standing order 缺少 --user-msg 流程"
CNT1=$(grep -c "CHIGUO-STANDING-ORDER-START" "$SO")
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
CNT2=$(grep -c "CHIGUO-STANDING-ORDER-START" "$SO")
[ "$CNT2" = "$CNT1" ] || fail "standing order 重复写入($CNT1→$CNT2)"

# ── 用例 5: 旧作业残留 → 先 rm 再 add ──
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; FAKE_HAS_OLD_JOB=1 bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "旧作业迁移期望 0 实得 $RC"
grep -q "automations rm chiguo-check-old" "$CALLS_LOG" || fail "未移除旧作业"
grep -q "automations add --name chiguo-check" "$CALLS_LOG" || fail "未注册新作业"

# ── 用例 6: .claude/settings.json 旧 hook 清除（含备份）──
printf '{"hooks":{"UserPromptSubmit":[{"hooks":[{"command":"~/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh"}]}]}}' \
  > "$TMP/repo/.claude/settings.json"
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "hook 清除期望 0 实得 $RC"
grep -q "UserPromptSubmit" "$TMP/repo/.claude/settings.json" && fail "hook 条目未清除" || true
[ -f "$TMP/repo/.claude/settings.json.bak" ] || fail "缺少 .bak 备份"

# ── 用例 7: 已注册后重跑 → 不重复 add ──
touch "$JOB_STATE"
CP1=$(grep -c "automations add --name chiguo-check" "$CALLS_LOG" || true)
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "重跑期望 0 实得 $RC"
CP2=$(grep -c "automations add --name chiguo-check" "$CALLS_LOG" || true)
[ "$CP2" = 0 ] && pass "幂等：重跑不重复 add（此前 $CP1 次）" || fail "重跑重复 add（$CP2 次）"

# ── 用例 8: 原生 hook / legacy handlers 自动处置 ──
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; FAKE_NATIVE_HOOKS="chiguo-old-hook" FAKE_LEGACY_HANDLERS='[{"matcher":""}]' \
  bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "原生 hook/legacy 处置期望 0 实得 $RC"
grep -q "hooks disable chiguo-old-hook" "$CALLS_LOG" || fail "未禁用旧原生 hook"
grep -q "doctor --fix" "$CALLS_LOG" || fail "未执行 doctor --fix（legacy handlers）"

# ── 用例 9: dry-run 有未完成工作 → 退出 1 且不执行修改 ──
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] && pass "dry-run 有待办 → 退出 1" || fail "dry-run 期望 1 实得 $RC"
grep -q "automations add --name" "$CALLS_LOG" && fail "dry-run 不应执行 add" || true
[ -f "$JOB_STATE" ] && fail "dry-run 不应写作业状态" || true

# ── 用例 10: dry-run 全已安装 → 退出 0 ──
touch "$JOB_STATE"
: > "$CALLS_LOG"
set +e; FAKE_TRIGGERS_ENABLED=true bash scripts/install_integration.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "dry-run 全已安装 → 退出 0" || fail "期望 0 实得 $RC"

echo "test_install_integration: 通过"
