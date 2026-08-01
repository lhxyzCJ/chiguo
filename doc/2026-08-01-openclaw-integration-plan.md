# OpenClaw 集成改造（v11 集成升级）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 OpenClaw 集成升级为官方原生机制——发送侧 trigger-script 条件门控（消灭 idle 空转模型调用）、回复侧 standing order 单记录（替代 Claude-Code hook），并交付可移植自包含安装器 `install_integration.sh`（严格校验 + 旧方案残留迁移）。

**Architecture:** OpenClaw automations 每 15 分钟无模型执行 `chiguo-watch.js`（跑 daemon --compact 解析 JSON），仅 `action=send` 时 `{fire:true}` 唤醒 main session agent 生成消息；回复侧由 standing order（agents/main/AGENTS.md）强制 agent 走「LLM 分析 → daemon --user-msg --analysis → SUN2.md 回复」流程。安装器按官方 CLI（`openclaw config/automations/hooks/doctor/security`）分级校验+迁移，任何机器 pull 后可自动引导。

**Tech Stack:** Node ≥20（trigger script + 单测）、Bash（安装器 + 桩测试）、Python 3.14/uv（既有 daemon，不改动）、OpenClaw CLI（目标机）。

**Spec:** `doc/2026-08-01-openclaw-integration-design.md`（已批准）

## Global Constraints

- 决策/生成分离铁律：daemon 只输出 JSON，绝不在 trigger script 中生成消息文本。
- 安全边界：standing order 只写 `$HOME/.openclaw/workspace/agents/main/AGENTS.md`；不触碰 workspace 根 AGENTS.md 与 `~/.openclaw/memory/`。
- 所有 CLI 命令与配置开关必须出自 docs.openclaw.ai（出处索引见 spec §8）；功能探测用 `<command> --help`，不用版本号。
- 安装器幂等可重跑；每次修改前备份；退出码 0=完成 / 1=警告或残留未处理 / 2=严重问题。
- 路径全部相对仓库根解析；机器相关路径单一事实来源为 `chiguo_proactive.toml`。
- 测试约定：独立 runner + 纯 assert + 失败非零退出；node 测试 `node test_trigger_script.js`，bash 测试 `bash test_install_integration.sh`。
- 不在本机修改真实 openclaw 配置（本机无 openclaw，测试用桩）；不 commit 运行时文件。

---

### Task 1: `scripts/chiguo-watch.js` + `test_trigger_script.js`

**Files:**
- Create: `scripts/chiguo-watch.js`
- Create: `test_trigger_script.js`
- Test: `node test_trigger_script.js`

**Interfaces:**
- Consumes: 无（独立）。OpenClaw 执行器提供全局 `tools`（`tools.call('exec', {command})`）、`json()`、`trigger`（含冻结的 `trigger.state`）——官方契约见 docs.openclaw.ai/automation/cron-jobs「Event triggers」。
- Produces: `module.exports = { parseDecision, decide }`（node 测试用）；环境变量 `CHIGUO_REPO` 可覆盖仓库根（默认 = 脚本目录上一级）。

- [ ] **Step 1: 写失败测试** `test_trigger_script.js`

```javascript
'use strict';
const assert = require('node:assert');

const SEND = { action: 'send', trigger: 'lonely_mid', intensity: 'medium',
               context: { instruction: '生成消息' }, msg_id: 'a1b2c3' };
const IDLE = { action: 'idle', reason: 'no_trigger', time: '2026-08-01T12:00:00+08:00' };
const BROKEN = '{"action": "send", broken';

let passed = 0;
function t(name, fn) { fn(); passed++; console.log(`  ok - ${name}`); }

const { parseDecision, decide } = require('./scripts/chiguo-watch.js');

t('parseDecision: 单行合法 JSON', () => {
  const d = parseDecision(JSON.stringify(SEND));
  assert.deepStrictEqual(d, SEND);
});
t('parseDecision: 空 stdout → null', () => {
  assert.strictEqual(parseDecision(''), null);
  assert.strictEqual(parseDecision('   \n\n'), null);
});
t('parseDecision: 损坏 JSON → null', () => {
  assert.strictEqual(parseDecision(BROKEN), null);
});
t('parseDecision: 多行缩进 JSON（daemon send 实际输出 indent=2）→ 解析成功', () => {
  const pretty = JSON.stringify(SEND, null, 2);
  assert.ok(pretty.includes('\n'), 'fixture 必须为多行');
  const d = parseDecision(pretty);
  assert.deepStrictEqual(d, SEND);
});
t('parseDecision: 前导空行 + 单行 JSON', () => {
  const d = parseDecision(`\n${JSON.stringify(IDLE)}\n`);
  assert.deepStrictEqual(d, IDLE);
});
t('parseDecision: 杂音行 + 单行 JSON（stdout 泄漏防护）→ 回退到 JSON 行', () => {
  const d = parseDecision(`some noise\n${JSON.stringify(IDLE)}`);
  assert.deepStrictEqual(d, IDLE);
});
t('decide: send → fire:true + 自包含 message', () => {
  const r = decide(SEND, {});
  assert.strictEqual(r.fire, true);
  assert.deepStrictEqual(JSON.parse(r.message), SEND);
  assert.strictEqual(r.state.last_error, undefined);
});
t('decide: idle → fire:false', () => {
  const r = decide(IDLE, {});
  assert.strictEqual(r.fire, false);
});
t('decide: null 决策（daemon 无输出）→ fire:false + last_error', () => {
  const r = decide(null, {});
  assert.strictEqual(r.fire, false);
  assert.match(r.state.last_error, /no decision/);
});
t('decide: 未知 action → fire:false + last_error', () => {
  const r = decide({ action: 'weird' }, {});
  assert.strictEqual(r.fire, false);
  assert.match(r.state.last_error, /unknown action/);
});
t('decide: idle 保留 prev state（last_error 清除）', () => {
  const r = decide(IDLE, { last_error: 'x', keep: 1 });
  assert.deepStrictEqual(r.state, { keep: 1 });
});
t('decide: send 清除 last_error', () => {
  const r = decide(SEND, { last_error: 'old' });
  assert.strictEqual(r.state.last_error, undefined);
});

(async () => {
  // ── run() 全链路（mock 全局 tools/json/trigger）──
  let captured = null;
  global.json = (r) => { captured = r; };
  global.tools = { call: async () => ({ result: { details: { aggregated: JSON.stringify(SEND) } } }) };
  global.trigger = { state: {} };
  delete require.cache[require.resolve('./scripts/chiguo-watch.js')];
  require('./scripts/chiguo-watch.js');
  await new Promise(r => setTimeout(r, 100));
  t('run(): mock exec 返回 send → fire:true', () => {
    assert.strictEqual(captured.fire, true);
    assert.deepStrictEqual(JSON.parse(captured.message), SEND);
  });

  captured = null;
  global.tools = { call: async () => ({ result: { details: { aggregated: '' } } }) };
  delete require.cache[require.resolve('./scripts/chiguo-watch.js')];
  require('./scripts/chiguo-watch.js');
  await new Promise(r => setTimeout(r, 100));
  t('run(): daemon 无输出 → fire:false + last_error', () => {
    assert.strictEqual(captured.fire, false);
    assert.match(captured.state.last_error, /no decision/);
  });

  captured = null;
  global.tools = { call: async () => { throw new Error('exec tool failed'); } };
  delete require.cache[require.resolve('./scripts/chiguo-watch.js')];
  require('./scripts/chiguo-watch.js');
  await new Promise(r => setTimeout(r, 100));
  t('run(): exec 抛错 → fire:false（不崩溃）', () => {
    assert.strictEqual(captured.fire, false);
    assert.match(captured.state.last_error, /exec tool failed/);
  });

  console.log(`test_trigger_script: ${passed}/15 passed`);
})().catch(e => { console.error('FAIL', e); process.exit(1); });
```

- [ ] **Step 2: 运行测试确认失败**

Run: `node test_trigger_script.js`
Expected: FAIL——`Cannot find module './scripts/chiguo-watch.js'`（文件尚不存在，非零退出）

- [ ] **Step 3: 写实现** `scripts/chiguo-watch.js`

```javascript
#!/usr/bin/env node
// chiguo-watch.js — OpenClaw automations trigger script（迟菓主动消息）
// 官方契约: docs.openclaw.ai/automation/cron-jobs → "Event triggers"
// 职责: 无模型执行 daemon --compact; 仅 action=send 时 fire 唤醒 agent。
// 容错: daemon 崩溃/坏 JSON/超时 → fire:false + state.last_error（下次评估带出）。
'use strict';

const path = require('node:path');

function parseDecision(stdout) {
  const text = String(stdout ?? '').trim();
  if (!text) return null;
  // 优先级: 全文（daemon send 输出为 indent=2 多行 JSON）→ 逐行回退（stdout 杂音泄漏防护）
  const candidates = [text, ...text.split('\n').map(l => l.trim()).filter(Boolean)];
  for (const c of candidates) {
    try {
      const obj = JSON.parse(c);
      if (obj && typeof obj === 'object') return obj;
    } catch { /* try next candidate */ }
  }
  return null;
}

function decide(decision, prevState) {
  const state = { ...(prevState && typeof prevState === 'object' ? prevState : {}) };
  if (!decision) {
    state.last_error = 'no decision JSON on stdout';
    return { fire: false, state };
  }
  if (decision.action === 'send') {
    delete state.last_error;
    return { fire: true, message: JSON.stringify(decision), state };
  }
  if (decision.action === 'idle') {
    delete state.last_error;
    return { fire: false, state };
  }
  state.last_error = `unknown action: ${decision.action}`;
  return { fire: false, state };
}

async function run() {
  const repo = String(process.env.CHIGUO_REPO || path.resolve(__dirname, '..')).replace(/\/+$/, '');
  const command = `${repo}/.venv/bin/python ${repo}/chiguo_daemon.py --compact`;
  const res = await tools.call('exec', { command });
  const aggregated = String(res?.result?.details?.aggregated ?? '').trim();
  const prevState = typeof trigger !== 'undefined' && trigger.state;
  json(decide(parseDecision(aggregated), prevState));
}

// OpenClaw 执行器提供 tools/json/trigger 全局 → 执行主流程；
// node 测试环境（无这些全局）→ 导出纯函数。
if (typeof tools !== 'undefined' && typeof json !== 'undefined') {
  run().catch(err => json({ fire: false, state: { last_error: String(err && err.message || err) } }));
} else if (typeof module !== 'undefined') {
  module.exports = { parseDecision, decide };
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `node test_trigger_script.js`
Expected: `test_trigger_script: 15/15 passed`，退出码 0

- [ ] **Step 5: 真实 daemon 冒烟（本机端到端）**

Run:
```bash
cd /root/chiguo && node -e "
const { parseDecision } = require('./scripts/chiguo-watch.js');
const { execFileSync } = require('node:child_process');
const out = execFileSync('./.venv/bin/python', ['chiguo_daemon.py', '--compact']).toString();
const d = parseDecision(out);
if (!d || !d.action) { console.error('FAIL: 真实 daemon 输出无法解析:', JSON.stringify(out.slice(0, 200))); process.exit(1); }
console.log('ok: parsed action=' + d.action + ' (输出 ' + out.length + ' bytes)');
"
```
Expected: `ok: parsed action=idle`（若状态恰好触发 send，则 action=send 且含 context 字段）。此步验证真实 stdout 契约（idle 单行 / send 多行缩进都能解析）

- [ ] **Step 6: Commit**

```bash
git add scripts/chiguo-watch.js test_trigger_script.js
git commit -m "feat: chiguo-watch trigger script + 15 用例（OpenClaw automations 官方契约）"
```

---

### Task 2: `scripts/install_integration.sh` + `test_install_integration.sh`

**Files:**
- Create: `scripts/install_integration.sh`
- Create: `test_install_integration.sh`
- Test: `bash test_install_integration.sh`

**Interfaces:**
- Consumes: Task 1 的 `$REPO_DIR/scripts/chiguo-watch.js`（注册时 `--trigger-script` 指向它）。
- Produces: 退出码 0/1/2；CLI 模式 `--dry-run`（默认非 TTY 时）、`--yes`、`--skip-integration`（由 deploy.sh 消费）；写 `$HOME/.openclaw/workspace/agents/main/AGENTS.md` 的 standing order 段落（带 `# CHIGUO-STANDING-ORDER-START/END` 标记）。

- [ ] **Step 1: 写失败测试** `test_install_integration.sh`

```bash
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
CP1=$(grep -c "automations add --name chiguo-check" "$CALLS_LOG")
: > "$CALLS_LOG"
set +e; bash scripts/install_integration.sh --yes >/dev/null 2>&1; RC=$?; set -e
[ "$RC" = 0 ] || fail "重跑期望 0 实得 $RC"
CP2=$(grep -c "automations add --name chiguo-check" "$CALLS_LOG")
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `bash test_install_integration.sh`
Expected: FAIL——`scripts/install_integration.sh` 不存在

- [ ] **Step 3: 写实现** `scripts/install_integration.sh`

```bash
#!/usr/bin/env bash
# ============================================================
# chiguo OpenClaw 集成安装/校验器（可移植：任意 pull 仓库的机器）
# 依据官方文档 docs.openclaw.ai（出处见 doc/OPENCLAW_INTEGRATION.md §九）
# 模式: --dry-run（只扫描报告）/ --yes（自动全部）/ 默认交互（非 TTY 等价 --dry-run）
# 退出码: 0=完成  1=有待办/警告/残留未处理  2=严重问题
# 幂等: 重复运行安全；每次修改前备份。
# ============================================================
set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CHIGUO_REPO="${CHIGUO_REPO_OVERRIDE:-$REPO_DIR}"   # 测试注入点；生产=仓库根
MODE=ask
[ -t 0 ] || MODE=dry-run
for a in "$@"; do
  case "$a" in
    --dry-run) MODE=dry-run ;;
    --yes) MODE=yes ;;
    --skip-integration) exit 0 ;;   # deploy.sh 传参时静默跳过
  esac
done

say() { printf '\033[1;32m[chiguo-integ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[chiguo-integ]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[chiguo-integ]\033[0m %s\n' "$*"; exit 2; }
DRY=0; [ "$MODE" = dry-run ] && DRY=1
PENDING=0    # 1 = 有待办/残留（dry-run 报告；yes/ask 完成后仍存在则退出 1）
PY="$(command -v python3 || echo "$REPO_DIR/.venv/bin/python")"   # JSON 编辑（stdlib 即可）

would() { [ "$DRY" = 1 ] && { PENDING=1; printf '  [dry-run] %s\n' "$*"; } || eval "$*"; }

# ── 阶段 0: 环境探测（官方：<command> --help 为权威清单）──────
if ! command -v openclaw >/dev/null 2>&1; then
  say "未检测到 openclaw → 跳过集成安装（daemon 独立可用；装好 OpenClaw 后重跑本脚本）"
  exit 0
fi
say "openclaw $(openclaw -V 2>&1 | head -1)"
if ! openclaw automations add --help 2>&1 | grep -q -- '--trigger-script'; then
  warn "当前版本不支持 automations --trigger-script（官方：<command> --help 为权威清单）"
  warn "降级路径：保留旧 cron system-event 方式 → 见 doc/OPENCLAW_INTEGRATION.md §八"
  exit 1
fi
say "功能探测通过：支持 --trigger-script"
if ! openclaw gateway status >/dev/null 2>&1; then
  warn "Gateway 状态未知/未运行：trigger 脚本由 Gateway 调度器执行，请先启动（openclaw gateway start）"
fi

# ── 阶段 0b: 旧方案残留扫描（发现即报告）──────────────────
echo "[chiguo-integ] 扫描旧方案残留 ..."
OLD_JOBS="$(openclaw automations list --all 2>/dev/null | grep -i chiguo || true)"
CLAUDE_SETTINGS="$CHIGUO_REPO/.claude/settings.json"
OLD_HOOK=0; [ -f "$CLAUDE_SETTINGS" ] && grep -q 'chiguo' "$CLAUDE_SETTINGS" && OLD_HOOK=1
ON_USER_MSG="$HOME/.openclaw/workspace/skills/chiguo/scripts/on-user-msg.sh"
CHIGUO_NATIVE_HOOKS="$(openclaw hooks list 2>/dev/null | grep -i chiguo || true)"
LEGACY_HANDLERS="$(openclaw config get hooks.internal.handlers 2>/dev/null || true)"
TRIGGERS_ENABLED="$(openclaw config get cron.triggers.enabled 2>/dev/null || true)"
[ -n "$OLD_JOBS" ] && warn "发现旧 automations 作业: $(echo "$OLD_JOBS" | tr '\n' ' ')"
[ "$OLD_HOOK" = 1 ] && warn "发现 .claude/settings.json 中 chiguo 的 UserPromptSubmit hook"
[ -f "$ON_USER_MSG" ] && warn "发现旧 hook 脚本: $ON_USER_MSG"
[ -n "$CHIGUO_NATIVE_HOOKS" ] && warn "发现 OpenClaw 原生 chiguo hook: $(echo "$CHIGUO_NATIVE_HOOKS" | tr '\n' ' ')"
[ -n "$LEGACY_HANDLERS" ] && warn "发现 legacy hooks.internal.handlers 配置（官方建议迁移）"

# ── 阶段 1: 配置开关（官方入口 config set + validate）───────
if [ "$TRIGGERS_ENABLED" != "true" ]; then
  echo "[chiguo-integ] 开启 cron.triggers.enabled（官方危险自动化开关：脚本以 agent 权限无头执行；本安装器仅注册 chiguo-watch.js 一条命令）"
  would "openclaw config set cron.triggers.enabled true"
else
  say "cron.triggers.enabled 已开启"
fi

# ── 阶段 2: 作业注册（幂等；先清旧作业）──────────────────
for job in $OLD_JOBS; do
  echo "[chiguo-integ] 移除旧作业 $job（由新 trigger-script 作业接管）"
  would "openclaw automations rm $job"
done
if ! openclaw automations get chiguo-check >/dev/null 2>&1; then
  INSTRUCTION="收到迟菓决策结果。按 SUN2.md 人格生成 1-3 句微信消息发给主人（当前微信会话上下文）。遵守 context.layer_guidance 语气指引与 context.instruction 格式约束；layer_guidance 含【安全阀】标记时语气务必温和克制。发送后运行 ${CHIGUO_REPO}/.venv/bin/python ${CHIGUO_REPO}/chiguo_daemon.py --record-send <msg_id> --text <消息原文> --trigger <trigger> --intensity <intensity>；发送失败则运行 --send-result <msg_id> --send-status failed。"
  would "openclaw automations add --name chiguo-check --every 15m --trigger-script '$CHIGUO_REPO/scripts/chiguo-watch.js' --session main --wake now --timeout-seconds 120 --system-event '$INSTRUCTION'"
else
  say "作业 chiguo-check 已存在，跳过注册"
fi

# ── 阶段 3: 回复侧 standing order（幂等标记段）────────────
SO_FILE="$HOME/.openclaw/workspace/agents/main/AGENTS.md"
if ! grep -qs 'CHIGUO-STANDING-ORDER-START' "$SO_FILE" 2>/dev/null; then
  if [ "$DRY" = 1 ]; then
    PENDING=1; echo "  [dry-run] 将写入 standing order 到 $SO_FILE"
  else
    mkdir -p "$(dirname "$SO_FILE")"
    [ -f "$SO_FILE" ] && cp -a "$SO_FILE" "$SO_FILE.bak"
    awk '/# CHIGUO-STANDING-ORDER-START/{f=1;next}/# CHIGUO-STANDING-ORDER-END/{f=0;next}!f' "$SO_FILE" 2>/dev/null > "$SO_FILE.tmp"
    cat >> "$SO_FILE.tmp" <<EOSO

# CHIGUO-STANDING-ORDER-START
## 迟菓消息流程（standing order，每会话注入）
每次收到主人的微信消息并准备回复时：
1. 用 LLM 分析主人消息情绪，输出 JSON：{"warmth": -1~1, "effort": 0~1, "attention": 0~1, "topic": "可选", "suppress_hours": 可选}
2. 运行 ${CHIGUO_REPO}/.venv/bin/python ${CHIGUO_REPO}/chiguo_daemon.py --user-msg <消息原文> --analysis '<JSON>'
3. 按 ~/.openclaw/workspace/skills/chiguo/SUN2.md 人格回复
4. 纪念日/假期指令：运行 chiguo_daemon.py --anniversary / --break 对应命令
# CHIGUO-STANDING-ORDER-END
EOSO
    mv "$SO_FILE.tmp" "$SO_FILE"
    say "standing order 已写入 $SO_FILE"
  fi
else
  say "standing order 已存在，跳过写入"
fi

# ── 阶段 3b: 清除 Claude-Code 式 hook / 旧脚本 / 旧 hook ──
if [ "$OLD_HOOK" = 1 ]; then
  if [ "$DRY" = 1 ]; then
    PENDING=1; echo "  [dry-run] 将备份并移除 $CLAUDE_SETTINGS 中的 chiguo hook 条目"
  else
    cp -a "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS.bak"
    "$PY" - "$CLAUDE_SETTINGS" <<'PYJ' || warn "hook 清除失败（.bak 已保留，请手工处理）"
import json, sys
p = sys.argv[1]
with open(p, encoding="utf-8") as f:
    cfg = json.load(f)
hooks = cfg.get("hooks", {})
ups = hooks.get("UserPromptSubmit", [])
kept = [e for e in ups if "chiguo" not in json.dumps(e)]
if len(kept) != len(ups):
    if kept:
        hooks["UserPromptSubmit"] = kept
    else:
        hooks.pop("UserPromptSubmit", None)
    cfg["hooks"] = hooks
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print("hook 条目已移除")
PYJ
  fi
else
  say "无 .claude/settings.json chiguo hook"
fi
if [ -f "$ON_USER_MSG" ]; then
  would "cp -a '$ON_USER_MSG' '$ON_USER_MSG.bak' && rm -f '$ON_USER_MSG'"
fi
if [ -n "$CHIGUO_NATIVE_HOOKS" ]; then
  for h in $CHIGUO_NATIVE_HOOKS; do
    echo "[chiguo-integ] 禁用旧 OpenClaw hook: $h"
    would "openclaw hooks disable $h"
  done
fi
if [ -n "$LEGACY_HANDLERS" ]; then
  echo "[chiguo-integ] legacy hooks.internal.handlers → 官方迁移工具"
  would "openclaw doctor --fix"
fi

# ── 阶段 4: 收尾验证 ──────────────────────────────────────
if [ "$DRY" = 1 ]; then
  [ "$PENDING" = 1 ] && exit 1
  say "dry-run：无待办（全部已安装）"
  exit 0
fi
openclaw config validate >/dev/null 2>&1 && say "config validate OK" || { warn "config validate 失败"; PENDING=1; }
if ! openclaw automations list 2>/dev/null | grep -q chiguo-check; then
  warn "作业 chiguo-check 未在册"; PENDING=1
else
  say "作业 chiguo-check 在册"
fi
if [ "$MODE" = yes ]; then
  echo "[chiguo-integ] 官方审计（危险自动化开关后）..."
  openclaw security audit --deep 2>&1 | tail -5
fi
[ "$PENDING" = 1 ] && exit 1
say "集成安装完成 ✓（端到端冒烟: openclaw automations run chiguo-check --wait --wait-timeout 10m）"
exit 0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `bash test_install_integration.sh`
Expected: 全部用例通过（`test_install_integration: 通过`，退出 0）。若有桩行为不匹配（如探测分支），调整桩或安装器——以官方 docs 为准，不得为过测试弱化校验。

- [ ] **Step 5: 本机 dry-run 自测（无 openclaw → 优雅降级）**

Run: `bash scripts/install_integration.sh --dry-run`
Expected: `未检测到 openclaw → 跳过集成安装`，退出 0，无副作用

- [ ] **Step 6: Commit**

```bash
git add scripts/install_integration.sh test_install_integration.sh
git commit -m "feat: install_integration.sh 安装器（严格校验 + 旧方案残留迁移）+ 桩测试"
```

---

### Task 3: `deploy.sh` 集成安装器调用

**Files:**
- Modify: `deploy.sh`（结尾 EOF 段之前插入调用；尾部引导文案更新）

**Interfaces:**
- Consumes: Task 2 的 `scripts/install_integration.sh`（`$PROJECT_DIR/scripts/install_integration.sh`，退出码 0/1/2，`--skip-integration` 跳过）。
- Produces: 无（仅编排）。

- [ ] **Step 1: 修改** `deploy.sh`

在第 4 步（envcheck 分支）之后、`# ── 6. 迁移提示` 之前插入：

```bash
# ── 5. OpenClaw 集成安装（可跳过: bash deploy.sh --skip-integration）──
if [[ "$*" != *--skip-integration* ]]; then
    say "安装 OpenClaw 集成（trigger-script 门控 + standing order）..."
    set +e
    bash "$PROJECT_DIR/scripts/install_integration.sh" "$@"
    IC=$?
    set -e
    case $IC in
        0) say "集成安装完成 ✓" ;;
        1) warn "集成安装有警告/残留未处理（见上方输出），daemon 部署不受影响" ;;
        2) fail "集成安装严重问题，请修复后重试（或 --skip-integration 跳过）" ;;
    esac
fi
```

并将尾部引导 `cat <<EOF` 中的 cron 注册块替换为：

```
OpenClaw 集成: 已由本脚本自动完成（trigger-script 门控 + standing order）
  手动重跑/排查: bash scripts/install_integration.sh --dry-run（扫描）| --yes（自动修复）
  端到端冒烟:   openclaw automations run chiguo-check --wait --wait-timeout 10m
  完整指南:     doc/OPENCLAW_INTEGRATION.md
```

- [ ] **Step 2: 语法与降级验证**

Run: `bash -n deploy.sh && bash -n scripts/install_integration.sh`
Expected: 无输出（语法 OK）
Run: `bash scripts/install_integration.sh --skip-integration; echo "rc=$?"`
Expected: 退出 0（skip 路径静默退出）

- [ ] **Step 3: Commit**

```bash
git add deploy.sh
git commit -m "chore: deploy.sh 接入 install_integration.sh（--skip-integration 可跳过）"
```

---

### Task 4: `doc/OPENCLAW_INTEGRATION.md` 重写

**Files:**
- Rewrite: `doc/OPENCLAW_INTEGRATION.md`

**Interfaces:**
- Consumes: Task 1/2/3 的成品路径与命令（脚本路径、automations 命令、standing order 内容、退出码约定）。
- Produces: 唯一权威集成指南（安装器 `--dry-run` 输出引用的「降级路径」章节在此）。

- [ ] **Step 1: 重写文档**（替换全文，结构如下；命令逐条对照官方 docs.openclaw.ai）

```markdown
# OpenClaw 集成指南（v11）

> 官方文档出处: docs.openclaw.ai（automations/cron-jobs、hooks、cli、doctor；功能探测以
> `<command> --help` 为权威清单）。旧版（v4）cron system-event + Claude-Code hook 方案见文末「降级路径」。

## 架构
（发送侧 trigger-script 门控数据流 + 回复侧 standing order 数据流，示意图同设计文档 §2）

## 一、安装（推荐：任意机器 pull 后自动引导）
bash scripts/install_integration.sh --dry-run   # 先扫描（只读）
bash scripts/install_integration.sh --yes       # 自动安装+迁移
bash deploy.sh                                  # 或随部署一起

## 二、安装器做了什么（逐阶段对应官方命令）
阶段0 探测: openclaw -V / automations add --help | grep --trigger-script / gateway status
阶段0b 残留扫描: automations list --all | grep -i chiguo / .claude/settings.json / hooks list / config get hooks.internal.handlers
阶段1 开关: openclaw config set cron.triggers.enabled true + config validate
阶段2 注册: openclaw automations add --name chiguo-check --every 15m --trigger-script <repo>/scripts/chiguo-watch.js --session main --wake now --timeout-seconds 120 --system-event "<指令>"
阶段3 standing order: agents/main/AGENTS.md（标记段幂等）
阶段3b 清理: .claude/settings.json hook 条目 / on-user-msg.sh / hooks disable / doctor --fix
阶段4 验证: automations list / config validate / security audit --deep / automations run --wait

## 三、trigger 脚本契约（scripts/chiguo-watch.js）
{fire, message?, state?}; idle→fire:false 零模型调用; send→fire:true+决策JSON; 容错→last_error

## 四、回复侧流程（standing order 内容全文）

## 五、特殊命令（--anniversary / --break，沿用 v4 表格）

## 六、管理命令
automations list/get/edit/disable/run --wait / enable / rm chiguo-check

## 七、调试
（沿用 v4 §六，更新为新命令）

## 八、降级路径（版本不支持 trigger-script 时）
1. openclaw automations add --name chiguo-check --cron "*/30 * * * *" --tz Asia/Shanghai \
     --session main --wake now --timeout-seconds 120 \
     --system-event "运行 python3 <repo>/chiguo_daemon.py。解析 stdout JSON。idle→NO_REPLY；send→SUN2.md 生成并发送"
2. 回复侧: 保留 standing order（同主方案，不依赖 hook）

## 九、官方出处索引（沿用设计文档 §8）
```

（实现时按此骨架写完整成文；指令文本与安装器保持一致）

- [ ] **Step 2: 交叉核对**（逐条）

对照官方文档页（automation/cron-jobs、automation/hooks、cli）核对文档中的每一条命令与参数；如有出入修正文档或安装器。
Run: `grep -n "openclaw " doc/OPENCLAW_INTEGRATION.md | wc -l`
Expected: 每条命令都能在官方 docs 中找到对应（人工核对，核对结果写入 commit message）

- [ ] **Step 3: Commit**

```bash
git add doc/OPENCLAW_INTEGRATION.md
git commit -m "docs: OPENCLAW_INTEGRATION v11 重写（trigger-script + standing order + 安装器 + 降级路径）"
```

---

### Task 5: 文档同步（README / doc/README / AGENTS.md / MEMORY.md / doc/IMPROVE.md）

**Files:**
- Modify: `README.md`、`doc/README.md`、`AGENTS.md`、`MEMORY.md`、`doc/IMPROVE.md`

**Interfaces:**
- Consumes: Task 1-4 的全部成品（文件清单、测试命令、集成方式）。
- Produces: 无。

- [ ] **Step 1: README.md** — 「测试」段加一行 `node test_trigger_script.js && bash test_install_integration.sh`；「集成」段一句话指向 doc/OPENCLAW_INTEGRATION.md + install_integration.sh

- [ ] **Step 2: doc/README.md** — 文件清单补 `scripts/chiguo-watch.js`、`scripts/install_integration.sh`、`test_trigger_script.js`、`test_install_integration.sh`、设计文档与实现计划

- [ ] **Step 3: AGENTS.md** — 测试链补 `node test_trigger_script.js && bash test_install_integration.sh`（放 py 链前）；「Architecture」补一句集成机制

- [ ] **Step 4: MEMORY.md** — 顶部新增条目：日期、背景、文件修改清单、验证结果（同既有 v10.1 格式）

- [ ] **Step 5: doc/IMPROVE.md** — 顶部新增 v11 集成改造记录（问题→方案→验证）

- [ ] **Step 6: Commit**

```bash
git add README.md doc/README.md AGENTS.md MEMORY.md doc/IMPROVE.md
git commit -m "docs: v11 集成改造文档同步（测试链/文件清单/MEMORY/IMPROVE）"
```

---

### Task 6: 全量验证 + 官方文档核对 + 代码审查

**Files:** 只读验证，无修改（审查发现问题则回到对应任务修复并补 commit）

**Interfaces:**
- Consumes: 全部任务成品。
- Produces: 审查结论记录（写进 MEMORY.md 条目或独立审查记录）。

- [ ] **Step 1: 全量回归**

Run: `node test_trigger_script.js && bash test_install_integration.sh && uv run python test_chiguo_math.py && uv run python test_holiday_parser.py && uv run python test_integration.py && uv run python test_monitor.py && uv run python test_eventbus.py && uv run python test_personality.py && uv run python test_bayesian.py && uv run python test_composer.py && uv run python test_ebbinghaus.py && uv run python test_longing.py && uv run python test_escape_valve.py && uv run python test_feedback.py && uv run python test_trigger.py && uv run python test_topics.py && uv run python test_circadian.py && uv run python test_followup.py && uv run python test_netease_proof.py && uv run python test_netease_service.py && uv run python test_envcheck.py`
Expected: 全部通过（19 py 文件 + 2 新测试）

- [ ] **Step 2: 官方文档最终核对**

逐条核对安装器与文档中的命令（automations add/rm/get/list/run、config get/set/validate、hooks list、security audit --deep、doctor --fix）与 docs.openclaw.ai 的 CLI 命令树与参数定义一致；记录核对表。

- [ ] **Step 3: 铁律要求的代码审查**

按 AGENTS.md：dispatch 并行审查子代理（仓库质量审查 + 官方文档合规审查），子代理继承主模型；审查发现的问题修复后补测试 + 补 commit + 更新 MEMORY.md。

- [ ] **Step 4: 最终提交（若审查有修复）与收尾确认**

```bash
git log --oneline -8
git status --short
```
Expected: 干净工作区；提交序列：trigger script → 安装器 → deploy.sh → 集成文档 → 文档同步 → （审查修复）
