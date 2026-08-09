# Agent RPC 常驻改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 chiguo 的 agent（pi 二进制）以 RPC 模式常驻，消灭 cron/每消息的 spawn 冷启动，最终形态 = 3 常驻进程（bridge + pi RPC 双会话 + daemon --loop）+ cron 仅剩 replan。版本号不动（保持 v1.11）。

**Architecture:** pi 的唯一常驻入口是 `--mode rpc`（stdio JSON-RPC，无 HTTP）。RPC 客户端 `wechat-bridge/agent-rpc.mjs` 已半落地（回复链 analysis 模式、握手/崩溃重启/失败回退 spawn）。本计划：①A 阶段把回复链 RPC 测试补全并默认启用；②B1 阶段把 RPC 扩展为双会话（chiguo-main 回复 / chiguo-send 主动发送），发送侧经 bridge 新增 HTTP 端点 `/agent/prompt` 转发（tick 是独立 cron/bash 进程，无法直连 stdio RPC，必须经常驻 bridge 转发）；③C 阶段 daemon `--loop` 转正：发送侧逻辑内聚进常驻循环，cron 只留 replan；④D 阶段文档补全 + 多 agent 审计。

**Tech Stack:** Node 22+（bridge/agent-rpc/agent-run）、Python 3.14（daemon/composer/state）、pi 二进制（`@earendil-works/pi-coding-agent` v0.84.1，`--mode rpc`）、systemd（chiguo-bridge.service）。

---

## 文件结构

| 文件 | 职责 | 改动 |
|---|---|---|
| `tests/fake-agent-rpc.mjs` | 新增：模拟 pi RPC 协议的 fake 二进制（stdin JSON 命令 → stdout NDJSON 事件） | Create |
| `tests/test_agent_rpc.mjs` | 新增：AgentRpc 契约测试（握手/prompt/失败/超时/崩溃/重启） | Create |
| `wechat-bridge/agent-rpc.mjs` | RPC 客户端：多会话 map + mode 分派 + key 轮换 | Modify |
| `scripts/agent-run.mjs` | 导出 `buildSendPrompt`；runSchedule 走共享参数路径 | Modify |
| `wechat-bridge/bridge.mjs` | `/agent/prompt` HTTP 端点（/send 同款鉴权）；RPC 默认启用 | Modify |
| `scripts/wechat-bridge.sh` | env 生成：`WECHAT_BRIDGE_AGENT_RUN` + `WECHAT_BRIDGE_AGENT_RPC=1` | Modify |
| `wechat-bridge/.env` | 修复旧文件名（`WECHAT_BRIDGE_PI_RUN` → 新名） | Modify（运行期产物） |
| `scripts/chiguo-tick.sh` | send 侧 agent 生成改走 bridge `/agent/prompt`（RPC 优先、回退 spawn） | Modify |
| `chiguo_daemon.py` | `--loop` 转正：send 分支内聚（调 bridge /agent/prompt + /send + record） | Modify |
| `scripts/install_agent.sh` | cron 只留 replan；新增 systemd chiguo-daemon.service | Modify |
| `deploy.sh` / `doc/DEPLOYMENT.md` | 部署拓扑与 systemd 同步 | Modify |
| `doc/SYSTEM.md` / `doc/AGENT_INTEGRATION.md` / `README.md` / `README_EN.md` | 架构拓扑、RPC 协议、环境变量文档 | Modify |

---

## 阶段 A：RPC 契约测试 + env 修复 + 默认启用

### Task A1: AgentRpc 契约测试（fake pi + 测试 runner）

**Files:**
- Create: `tests/fake-agent-rpc.mjs`
- Create: `tests/test_agent_rpc.mjs`

- [ ] **Step 1: 写 fake pi（模拟 `--mode rpc` stdio 协议）**

```js
#!/usr/bin/env node
// tests/fake-agent-rpc.mjs — 模拟 pi --mode rpc：stdin JSON 命令 → stdout NDJSON 事件。
// 契约（pi dist/modes/rpc）：get_state 回 response；prompt 先回 preflight response
// （success:true），再流式输出 message_end（含 text）与 agent_settled（回合完成）。
import readline from 'node:readline'
const rl = readline.createInterface({ input: process.stdin })
rl.on('line', (line) => {
  let cmd
  try { cmd = JSON.parse(line) } catch { return }
  if (cmd.type === 'get_state') {
    process.stdout.write(JSON.stringify({ type: 'response', id: cmd.id, command: 'get_state', success: true }) + '\n')
  } else if (cmd.type === 'prompt') {
    // preflight 即回（与真实 pi 一致）
    process.stdout.write(JSON.stringify({ type: 'response', id: cmd.id, command: 'prompt', success: true }) + '\n')
    // 回合事件：message_end（analysis 形状）+ agent_settled
    setTimeout(() => {
      const text = '<<ANALYSIS>>{"warmth":0.5,"effort":0.6,"attention":0.7}<<END>> 测试回复'
      process.stdout.write(JSON.stringify({
        type: 'message_end',
        message: { content: [{ type: 'text', text }], usage: { prompt_tokens: 10, completion_tokens: 5 } },
      }) + '\n')
      process.stdout.write(JSON.stringify({ type: 'agent_settled' }) + '\n')
    }, 20)
  }
})
```

- [ ] **Step 2: 写契约测试**

```js
// tests/test_agent_rpc.mjs — AgentRpc 契约测试（fake pi 注入 bin，不 spawn 真 pi）
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
const root = join(dirname(fileURLToPath(import.meta.url)), '..')
process.chdir(root)
process.env.AGENTRUN_TELEMETRY = '0'
const { AgentRpc } = await import('../wechat-bridge/agent-rpc.mjs')
const FAKE = join(root, 'tests', 'fake-agent-rpc.mjs')

async function withRpc(fn) {
  const rpc = new AgentRpc({ bin: process.execPath, args: [FAKE] })
  try { await fn(rpc) } finally { rpc.dispose() }
}

async function test_handshake_ok() {
  await withRpc(async (rpc) => { await rpc.ensureStarted(); assert(rpc.proc && !rpc.dead) })
  console.log('  OK test_handshake_ok')
}
async function test_prompt_returns_text_and_analysis() {
  await withRpc(async (rpc) => {
    const r = await rpc.prompt('哥哥在吗')
    assert(r.text.includes('测试回复'), 'text 应来自 message_end')
    assert(r.analysis && r.analysis.warmth === 0.5, 'analysis 应被解析')
  })
  console.log('  OK test_prompt_returns_text_and_analysis')
}
async function test_crash_auto_restart() {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const pid = rpc.proc.pid
    rpc.proc.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 300))
    const r = await rpc.prompt('重启后')
    assert(r.text.includes('测试回复'), '崩溃后下一轮应自动重启')
    assert(rpc.proc.pid !== pid)
  })
  console.log('  OK test_crash_auto_restart')
}
async function test_restart_kills_process() {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const pid = rpc.proc.pid
    rpc.restart()
    assert(rpc.dead === true)
  })
  console.log('  OK test_restart_kills_process')
}

function assert(cond, msg = 'assert failed') { if (!cond) throw new Error(msg) }
const tests = [test_handshake_ok, test_prompt_returns_text_and_analysis, test_crash_auto_restart, test_restart_kills_process]
for (const t of tests) await t()
console.log(`\n${'='.repeat(40)}\nALL ${tests.length} tests passed.`)
```

- [ ] **Step 3: 实现 AgentRpc 支持 `args` 注入（fake bin 需要）**

`wechat-bridge/agent-rpc.mjs:28` 构造签名 `{ bin }` → `{ bin, args = [] }`，`:58` spawn 改为 `spawn(this.bin, [...this.args, '--mode', 'rpc', ...buildBaseAgentArgs(...)])`。

- [ ] **Step 4: 跑测试（期望失败 → 通过）**

Run: `node tests/test_agent_rpc.mjs`
Expected: 先 FAIL（AgentRpc 不认 args 注入）→ 修复后 ALL 4 tests passed

- [ ] **Step 5: 注册进 ci-test.sh 与 commit**

`scripts/ci-test.sh` 的 mjs 段追加 `node tests/test_agent_rpc.mjs`（5 mjs → 6 mjs；`test_docs_sync.py` 计数同步 42 py + 10 script → 42 py + 11 script，AGENTS.md/CLAUDE.md/README/README_EN 同步）。

### Task A2: env 修复 + 默认启用回复链 RPC

**Files:**
- Modify: `scripts/wechat-bridge.sh`
- Modify: `wechat-bridge/bridge.mjs:43-45`
- Test: `tests/test_bridge_askagent.mjs`

- [ ] **Step 1: wechat-bridge.sh env 生成补新键**

`scripts/wechat-bridge.sh` install 段（:53-56 附近）追加写入：
```bash
WECHAT_BRIDGE_AGENT_RUN=$PROJECT_DIR/scripts/agent-run.mjs
WECHAT_BRIDGE_AGENT_RPC=1
```
（移除旧名 `WECHAT_BRIDGE_PI_RUN` 的写入；`.env` 由 install 重新生成。）

- [ ] **Step 2: 修复现有 .env**

Run: `bash scripts/wechat-bridge.sh install` 重新生成 `.env`；验证 `grep AGENT_RUN wechat-bridge/.env` 出现 `WECHAT_BRIDGE_AGENT_RUN` 且无 `WECHAT_BRIDGE_PI_RUN`。

- [ ] **Step 3: 测试：askAgent 在 RPC 启用时走 RPC 分支、失败回退 spawn**

`tests/test_bridge_askagent.mjs` 增加两个用例（沿用现有 runAll 模式 + `_makeAgentStub` fake 注入）：
```js
// RPC 启用 + RPC 成功 → 返回 {text, analysis}，不 spawn agent-run
// RPC 启用 + RPC 抛错 → 回退 spawn（现有断言复用）
```
实现：bridge.mjs `askAgent` 的 RPC 分支加 `globalThis.__agentRpc = null` 重置钩子便于测试隔离（当前 `:84` 单例缓存）。

- [ ] **Step 4: 跑测试与 commit**

Run: `node tests/test_bridge_askagent.mjs` → 全过；`node --check wechat-bridge/bridge.mjs scripts/wechat-bridge.sh`（语法）；commit。

---

## 阶段 B1：RPC 多会话 + runSchedule 重构 + /agent/prompt 端点

### Task B1-1: runSchedule 重构走 buildBaseAgentArgs

**Files:**
- Modify: `scripts/agent-run.mjs:297-356`

- [ ] **Step 1: 重构**：`runSchedule` 内 extract/verify/recall/replan 四处 `spawn(bin, [...])` 改为统一经 `buildBaseAgentArgs({ analysisMode })` 组合 + `--session-id <对应会话>`（会话名保持 `chiguo-extract/verify/recall/replan` 不变，`:299-300`），参数顺序与既有 print 模式一致。
- [ ] **Step 2: 回归**：Run: `node tests/test_agent_run.mjs` 全绿（行为不变，纯参数路径重构）。
- [ ] **Step 3: commit**

### Task B1-2: AgentRpc 多会话（chiguo-main / chiguo-send）

**Files:**
- Modify: `scripts/agent-run.mjs`（导出 `buildSendPrompt`）
- Modify: `wechat-bridge/agent-rpc.mjs`
- Test: `tests/test_agent_rpc.mjs`（扩展）

- [ ] **Step 1: 导出 send 模板**：agent-run.mjs `run()` 内 send 分支模板（:238-239）提取为导出函数：
```js
export function buildSendPrompt(decisionJson) {
  return `你是迟菓。以下是主动消息决策结果 JSON（action=send）。按迟菓人格与 context 中的 layer_guidance/instruction 生成 1-3 句微信消息发给哥哥，自然、不汇报、不打破第四面墙。\n\n决策：${decisionJson}`
}
```
`run()` 内 send 分支改为调用它（行为不变）。

- [ ] **Step 2: AgentRpc 多会话**：构造函数接受 `{ sessionId = 'chiguo-main', mode = 'analysis', bin, args }`；`prompt(message, { mode } = {})` 按 mode 选模板（analysis → `buildAnalysisPrompt`；send → `buildSendPrompt`）；内部 `this.sessions = new Map()` 按 `sessionId|mode` 键管理多个常驻进程（每个进程 `--session-id <id>` + `--thinking` 按 mode 选 `REPLY_THINKING`/`THINKING`）；`prompt` 失败自动重启仅作用于对应会话进程；`restart()` 保留（全部会话重启，供 /new 与 key 轮换）。

- [ ] **Step 3: 扩展测试**：`test_agent_rpc.mjs` 增加：
```js
// send 模式：prompt('{...decision...}', {mode:'send'}) → text 来自 buildSendPrompt 模板包裹后 fake 回显
// 双会话隔离：analysis 与 send 各自独立进程（proc.pid 不同）、互不 pending 冲突
```
- [ ] **Step 4: 回归 + commit**：`node tests/test_agent_rpc.mjs` + `node tests/test_agent_run.mjs` 全绿。

### Task B1-3: bridge `/agent/prompt` HTTP 端点

**Files:**
- Modify: `wechat-bridge/bridge.mjs`（HTTP server 段，/send 同款鉴权 :294-330）
- Test: `tests/test_bridge_askagent.mjs`（或新 `tests/test_bridge_agent_http.mjs`）

- [ ] **Step 1: 端点实现**：
```js
// POST /agent/prompt {"text","mode":"analysis|send"} → AgentRpc.prompt(text,{mode})
// 鉴权与 /send 一致：本地回环（Host/Origin 127.0.0.1/localhost/::1）+ 可选 token（/send 共享）。
// 成功 → {"ok":true,"text","analysis"?}；失败 → {"ok":false,"error"} + 503（调用方回退 spawn）。
```
复用 `deny()`（:317）与 token 校验（:47-48 共享 token 逻辑）；模式白名单 `['analysis','send']`。
- [ ] **Step 2: 测试**：HTTP 用例（起 bridge server 于随机端口，`fetch` POST `/agent/prompt`）：RPC 成功路径返回 text/analysis；非法 mode → 400；错误 → 503；鉴权（Host 非回环）→ 拒绝。
- [ ] **Step 3: 回归 + commit**：`node tests/test_bridge_askagent.mjs` + 新测试全绿。

### Task B1-4: tick 发送侧接入（RPC 优先、回退 spawn）

**Files:**
- Modify: `scripts/chiguo-tick.sh:64`（agent 生成段）

- [ ] **Step 1: 改发送生成**：`chiguo-tick.sh` 的 agent-run spawn 之前先试 bridge HTTP：
```bash
# 发送侧：优先常驻 RPC（经 bridge /agent/prompt 转发，send 会话），失败回退 spawn
OUT_JSON="$OUT"  # decision JSON（tick 已解析 action=send）
RPC_OUT=$(curl --max-time 125 --noproxy '*' -s -X POST "$BRIDGE_URL/agent/prompt" \
  -H 'Content-Type: application/json' \
  -d "{\"text\": $(python -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$OUT_JSON"), \"mode\": \"send\"}")
# 解析 ok/text；ok=true 直接用 text；否则走原 node agent-run.mjs --send-mode 回退
```
（`python -c` 内联 JSON 转义——chiguo 风格改用 `--user-msg-file` 式文件传递更稳，见 Step 2。）
- [ ] **Step 2: 文件传递**：decision JSON 写临时文件 → curl `--data-binary @file` + `"text":` 改为读文件字段（curl `-d @file` 不支持嵌套 JSON；改用 python `json.dumps` 生成 body 落盘再 `-d @body.json`），保持与 composer 兜底共用 `mktemp` 文件。
- [ ] **Step 3: 验证**：`tests/test_tick_health.sh` 补 send 分支用例（RPC 端点 mock 返回文本 → tick 不发 spawn；端点 503 → 回退 spawn 路径断言）。
- [ ] **Step 4: commit**

---

## 阶段 C：daemon --loop 转正

### Task C1: --loop send 分支内聚

**Files:**
- Modify: `chiguo_daemon.py:1639-1659`（loop 的 run()）

- [ ] **Step 1: run() send 分支内聚**：loop 模式（`args.loop`）下 `decision["action"] == "send"` 时不再只打印 JSON，改为内联执行"生成 → 发送 → 记账"：
```python
# --loop 转正：send 分支内聚（替代 cron tick.sh 的 send 动作）
if decision["action"] == "send":
    # 1. agent 生成（经 bridge /agent/prompt RPC 优先；失败回退 spawn agent-run）
    # 2. bridge /send 发送
    # 3. --record-send 记账（复用现有 send_record 逻辑/refund）
```
实现为 `DecisionEngine` 新方法 `_loop_send(decision, cfg)`（HTTP 用 urllib stdlib；回退 spawn 复用 `scripts/agent-run.mjs` 命令行，与 tick.sh 同构）；异常捕获 → 记 decision JSON 到 stdout（保持 `--loop` 原有可观测性）+ `record_health fail` 语义由调用侧保留。
- [ ] **Step 2: 配置**：toml 新增 `[loop]` 段：`bridge_url = "http://127.0.0.1:18790"`、`agent_timeout_ms = 120000`（默认值即现 tick 常量）。
- [ ] **Step 3: 测试**：`tests/test_loop_send.py`（新增）：构造 DecisionEngine + fake bridge HTTP server（python `http.server` 线程）：send 决策 → 生成请求打到 `/agent/prompt`（断言 body mode=send）→ `/send` 被调 → record 记账；bridge 503 → 回退 spawn 断言（spawn 路径 mock）；异常 → 决策 JSON 仍输出且不崩溃。
- [ ] **Step 4: 注册 ci-test.sh（42 py → 43 py）+ 文档计数同步 + commit**

### Task C2: 部署切换（cron 只留 replan + systemd）

**Files:**
- Modify: `scripts/install_agent.sh`（cron 注册段 :155-217）
- Modify: `deploy.sh`、`doc/DEPLOYMENT.md`

- [ ] **Step 1: cron 调整**：tick 条目（:75）注册改为可选（`CHIGUO_LOOP=1` 时跳过 tick 条目，仅 replan :190 保留）；loop 常驻由 systemd 守护。
- [ ] **Step 2: systemd 单元**：新增 `scripts/chiguo-daemon.service`（`ExecStart=.../.venv/bin/python .../chiguo_daemon.py --loop 900 --compact`，`Restart=on-failure`，`RestartSec=10`），install_agent.sh 阶段 7 安装（参照 bridge service 模板）；`.env` 语义：loop 模式读 `[loop]` 段而非 WECHAT_BRIDGE_*。
- [ ] **Step 3: deploy.sh 同步**：`--skip-loop` 标志（与既有 --skip-* 风格一致）+ DEPLOYMENT.md 部署拓扑章节更新（3 常驻进程 + cron replan）。
- [ ] **Step 4: 验证 + commit**：`bash scripts/ci-test.sh` 全绿（deploy.sh 的 --skip-* 断言测试同步）。

### Task C3: 状态一致性验证

**Files:**
- Create: `tests/test_loop_concurrency.py`

- [ ] **Step 1: 并发写验证测试**：两个进程同时 evaluate/record_user_message（子进程 + 共享 state 文件），断言：state.json 校验和通过、tick_seq 单调、无锁竞争异常（复用 `chiguo_state.json.lock` flock 机制，`chiguo_state.py:541-617`）。
- [ ] **Step 2: 常驻态热重载验证**：loop 进程运行中修改 toml → 下一轮 `_maybe_reload_config` 生效（构造性验证，不真跑 loop 长循环，单轮 `_maybe_reload_config` 调用断言）。
- [ ] **Step 3: 注册 ci-test.sh（43 py → 44 py）+ 文档计数同步 + commit**

---

## 阶段 D：文档补全 + 审计 + push

### Task D1: 文档补全

- `doc/SYSTEM.md`：架构章节（§1）更新部署拓扑（bridge 常驻 + pi RPC 双会话常驻 + daemon --loop 常驻 + cron 仅 replan）；新增 §"Agent RPC 常驻"小节（协议、双会话、/agent/prompt 端点、key 轮换语义、回退链）。
- `doc/AGENT_INTEGRATION.md`：RPC 常驻章节补全（env 变量、多会话、端点契约、故障排查）；修正改名残留（PI_RUN 等）。
- `doc/DEPLOYMENT.md`：systemd 单元表（chiguo-bridge / netease-api / chiguo-daemon）、cron 表（仅 replan）、env 表。
- `README.md` / `README_EN.md`：架构图与部署章节同步（中英对齐，test_docs_sync 保绿）。
- 测试计数同步：`test_docs_sync.py` / `AGENTS.md` / `CLAUDE.md` / `CLAUDE_CODE_RULES.md` 的 "44 py + 11 script"。

### Task D2: 多 agent 全量审计

- 派发并行审查：代码质量（review）、死代码/过时代码、文档一致性（参照 v1.11 流程）。
- 修复发现的问题，回归全链。

### Task D3: 全链测试 + merge + push

- Run: `bash scripts/ci-test.sh` → ALL TESTS PASSED（44 py + 11 script）。
- 版本号不动（`chiguo_version.py` 保持 `1.11`）。
- PR squash merge → push origin main → 关闭 Issue #107。
