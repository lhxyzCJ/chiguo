#!/usr/bin/env bash
# install_integration.sh 桩测试：假 openclaw + 临时 HOME，验证各阶段行为与退出码（14 用例）
set -euo pipefail
TMP="$(mktemp -d /tmp/chiguo-install-test.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
pass() { echo "  ok - $*"; }

# ── 假 openclaw：有状态（JOB_STATE + CONFIG_TRIGGER_STATE）+ 调用记录 calls.log；行为由 FAKE_* 控制 ──
mkdir -p "$TMP/bin" "$TMP/home" "$TMP/repo/.claude"
cat > "$TMP/bin/openclaw" <<'STUB'
#!/usr/bin/env bash
echo "$0 $*" >> "$CALLS_LOG"
case "$1" in
  -V) echo "v2026.8.1-test" ;;
  cron)
    case "$2" in
      add)
        if echo "$*" | grep -q -- "--help"; then echo "--trigger-script"
        elif [ "${FAKE_ADD_FAIL:-0}" = 1 ]; then echo "error"; exit 1
        else echo "job created"; touch "$JOB_STATE"; fi ;;
      rm) echo "removed"; case "$3" in
            chiguo-check) rm -f "$JOB_STATE" ;;   # 旧作业(如 chiguo-check-old)不影响在册作业状态
          esac ;;
      get) [ -f "$JOB_STATE" ] && echo "{\"name\":\"chiguo-check\",\"enabled\":true}" || exit 1 ;;
      list)
        if [ "${FAKE_LIST_FORMAT:-line}" = "table" ]; then
          [ -f "$JOB_STATE" ] && echo "chiguo-check  enabled  */15"
          [ "${FAKE_HAS_OLD_JOB:-0}" = 1 ] && echo "chiguo-check-old  enabled  */30"
        else
          if [ -f "$JOB_STATE" ]; then echo "chiguo-check";
          elif [ "${FAKE_HAS_OLD_JOB:-0}" = 1 ]; then echo "chiguo-check-old"; fi
        fi ;;
      run) echo "run ok" ;;
    esac ;;
  config)
    case "$2" in
      get) case "$3" in
             cron.triggers.enabled)
               if [ -n "${FAKE_TRIGGERS_ENABLED+x}" ]; then echo "$FAKE_TRIGGERS_ENABLED"
               elif [ -f "$CONFIG_TRIGGER_STATE" ]; then echo "true"
               else echo "false"; fi ;;
             hooks.internal.handlers) echo "${FAKE_LEGACY_HANDLERS:-}" ;;
             *) echo "" ;;
           esac ;;
      set) echo "set ok"
           case "$3" in
             cron.triggers.enabled) echo true > "$CONFIG_TRIGGER_STATE" ;;
           esac ;;
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
export CONFIG_TRIGGER_STATE="$TMP/config_trigger_state"
export PATH="$TMP/bin:$PATH" HOME="$TMP/home"
export CHIGUO_REPO_OVERRIDE="$TMP/repo"   # 安装器读此环境变量指向测试仓库根

# ── 隔离工具目录（不含 openclaw）：用例 1 模拟"无 openclaw 的机器" ──
mkdir -p "$TMP/sysbin"
for t in bash printf read command grep awk sed cat cp mkdir mv rm python3 node dirname; do
  ln -sf "$(command -v "$t")" "$TMP/sysbin/$t" 2>/dev/null || true
done

# ── 用例 1: 无 openclaw → 跳过集成，退出 0 ──
rm -f "$JOB_STATE"
set +e; PATH="$TMP/sysbin" bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
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
grep -q "cron add chiguo-check" "$CALLS_LOG" || fail "未注册新作业"
grep -q -- "--trigger-script /tmp/chiguo-watch." "$CALLS_LOG" || fail "注册未传 sed 替换后的临时触发器文件"
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
grep -q "cron rm chiguo-check-old" "$CALLS_LOG" || fail "未移除旧作业"
grep -q "cron add chiguo-check" "$CALLS_LOG" || fail "未注册新作业"

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
CP1=$(grep -c "cron add chiguo-check" "$CALLS_LOG" || true)
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "重跑期望 0 实得 $RC"
CP2=$(grep -c "cron add chiguo-check" "$CALLS_LOG" || true)
[ "$CP2" = 0 ] && pass "幂等：重跑不重复 add（此前 $CP1 次）" || fail "重跑重复 add（$CP2 次）"

# ── 用例 8: 原生 hook 禁用；legacy handlers 只检测告警（不再自动 doctor --fix），残留计 PENDING ──
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; OUT=$(FAKE_NATIVE_HOOKS="chiguo-old-hook" FAKE_LEGACY_HANDLERS='[{"matcher":""}]' \
  bash scripts/install_integration.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "legacy handlers 残留期望 1（PENDING）实得 $RC"
grep -q "hooks disable chiguo-old-hook" "$CALLS_LOG" || fail "未禁用旧原生 hook"
grep -q "doctor --fix" "$CALLS_LOG" && fail "legacy handlers 不应再自动 doctor --fix" || true
echo "$OUT" | grep -q "迁移到 discovery 系统" || fail "缺少 legacy handlers 迁移提示"
pass "legacy handlers：检测告警 + PENDING=1，不自动 doctor --fix"

# ── 用例 9: dry-run 有未完成工作 → 退出 1 且不执行修改 ──
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 1 ] && pass "dry-run 有待办 → 退出 1" || fail "dry-run 期望 1 实得 $RC"
grep -q "cron add chiguo-check" "$CALLS_LOG" && fail "dry-run 不应执行 add" || true
[ -f "$JOB_STATE" ] && fail "dry-run 不应写作业状态" || true

# ── 用例 10: dry-run 全已安装 → 退出 0 ──
touch "$JOB_STATE"
: > "$CALLS_LOG"
set +e; FAKE_TRIGGERS_ENABLED=true bash scripts/install_integration.sh --dry-run >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] && pass "dry-run 全已安装 → 退出 0" || fail "期望 0 实得 $RC"

# ── 用例 11: 无标志 + stdin 非终端 → 默认 dry-run（不进 ask/read）──
rm -f "$JOB_STATE"
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_integration.sh < /dev/null 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "非 TTY 默认 dry-run 期望 1 实得 $RC"
echo "$OUT" | grep -q 'dry-run' || fail "非 TTY 应走 dry-run 路径"
echo "$OUT" | grep -q '\[ask\]' && fail "非 TTY 不应进入 ask 模式" || true
grep -q "cron add chiguo-check" "$CALLS_LOG" && fail "默认模式不应执行 add" || true
pass "非 TTY 默认 dry-run（无 ask 确认）"

# ── 用例 12: hook JSON 清除失败（损坏文件）→ 警告 + 退出 1 ──
rm -f "$JOB_STATE"
printf '{"hooks":{"UserPromptSubmit":[{"command":"chiguo"' > "$TMP/repo/.claude/settings.json"
: > "$CALLS_LOG"
set +e; OUT=$(bash scripts/install_integration.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "hook 清除失败期望 1 实得 $RC"
echo "$OUT" | grep -q 'hook 清除失败' || fail "缺少 hook 清除失败警告"
[ -f "$TMP/repo/.claude/settings.json.bak" ] || fail "缺少 .bak 备份"
pass "hook 清除失败 → 警告 + 退出 1"

# ── 用例 13: 表格式 list 输出（多列行）→ 只取第一列，不误动新作业，幂等不重复 add ──
rm -f "$TMP/repo/.claude/settings.json"   # 用例 12 留下的损坏文件，避免误判 OLD_HOOK
touch "$JOB_STATE"
: > "$CALLS_LOG"
set +e; FAKE_LIST_FORMAT=table FAKE_HAS_OLD_JOB=1 FAKE_NATIVE_HOOKS="chiguo-old-hook  enabled  UserPromptSubmit" \
  bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "表格式 list 期望 0 实得 $RC"
grep -q "cron rm chiguo-check-old" "$CALLS_LOG" || fail "表格式 list 未 rm 旧作业 chiguo-check-old"
grep -q "cron rm chiguo-check\$" "$CALLS_LOG" && fail "表格式 list 误 rm 新作业 chiguo-check" || true
grep -q "hooks disable chiguo-old-hook\$" "$CALLS_LOG" || fail "表格式 hooks list 未禁用 chiguo-old-hook"
CP=$(grep -c "cron add chiguo-check" "$CALLS_LOG" || true)
[ "$CP" = 0 ] && pass "表格式 list：仅动旧作业，幂等不重复 add" || fail "表格式 list 重复 add（$CP 次）"

# ── 用例 14: config set 失败模拟（FAKE_TRIGGERS_ENABLED=false 固定 get 值）→ 阶段 4 复验警告 + 退出 1 ──
rm -f "$JOB_STATE" "$CONFIG_TRIGGER_STATE" "$HOME/.openclaw/workspace/agents/main/AGENTS.md"
: > "$CALLS_LOG"
set +e; OUT=$(FAKE_TRIGGERS_ENABLED=false bash scripts/install_integration.sh --yes 2>&1); RC=$?; set -e
[ "$RC" = 1 ] || fail "triggers set 失败期望 1 实得 $RC"
echo "$OUT" | grep -q "cron.triggers.enabled" || fail "缺少 triggers 复验警告"
echo "$OUT" | grep -q "config set 可能失败" || fail "复验警告未说明 set 失败归因"
pass "triggers set 失败 → 阶段 4 复验警告 + 退出 1"

echo "test_install_integration: 通过"
