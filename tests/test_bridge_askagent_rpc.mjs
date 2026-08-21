#!/usr/bin/env node
/**
 * tests/test_bridge_askagent_rpc.mjs — bridge askAgent 的 RPC 分支接线测试。
 * 场景：AGENT_RPC_ENABLED=1 时：
 *   1) 预置可用 AgentRpc（fake pi）→ askAgent 走 RPC，不 spawn agent-run
 *   2) 预置抛错的 AgentRpc stub → 回退 spawn（fake agent-run 被调）
 * 复用 fake agent-run / fake daemon 模式（与 test_bridge_askagent.mjs 同构）。
 */
import assert from 'node:assert'
import { writeFileSync, mkdtempSync, appendFileSync, cpSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'bridge-askagent-rpc-'))
const FAKE_AGENT = join(tmp, 'fake-agent-run.mjs')
const FAKE_DAEMON = join(tmp, 'fake-daemon.mjs')
const FAKE_PI = join(tmp, 'fake-pi.mjs')
const AGENT_LOG = join(tmp, 'agent.log')
const DAEMON_LOG = join(tmp, 'daemon.log')

// O2: 防写真实 logs/agent-run.log
process.env.AGENTRUN_TELEMETRY = '0'
// R3: 干净 HOME 注入——AgentRpc 构造会 mkdir PID_DIR + _killStale() 扫描 ~/.pi/agent,
// 不隔离会杀掉线上 bridge 的常驻 rpc 子进程(homeDir() 在模块顶层求值,须在 import 前设置)。
const prevHome = process.env.HOME
const cleanHome = join(tmpdir(), `chiguo-bridge-askagent-rpc-${process.pid}`)
process.env.HOME = cleanHome
process.on('exit', () => {
  if (prevHome === undefined) delete process.env.HOME
  else process.env.HOME = prevHome
  rmSync(cleanHome, { recursive: true, force: true })
})

const PH_SCRIPT = join(tmp, 'agent_health.py')
cpSync(new URL('../scripts/agent_health.py', import.meta.url).pathname, PH_SCRIPT)
process.env.WECHAT_BRIDGE_AGENT_HEALTH = PH_SCRIPT

writeFileSync(FAKE_AGENT, `
import { appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_AGENT_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
process.stdout.write(JSON.stringify({ ok: true, text: 'spawn 回复' }))
`)

writeFileSync(FAKE_DAEMON, `
import { appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_DAEMON_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
if (process.argv[2] === '--attention') {
  process.stdout.write(JSON.stringify({ action: 'attention', ok: false, reason: 'fake 无注意力块' }))
} else {
  process.stdout.write(JSON.stringify({ action: 'recorded', ok: true }))
}
process.exit(Number(process.env.FAKE_DAEMON_EXIT ?? 0))
`)

// fake pi（--mode rpc 协议）：get_state 握手 + prompt preflight 即回 + message_end(analysis 形状) + agent_settled
writeFileSync(FAKE_PI, `
import readline from 'node:readline'
const rl = readline.createInterface({ input: process.stdin })
rl.on('line', (line) => {
  let cmd
  try { cmd = JSON.parse(line) } catch { return }
  if (cmd.type === 'get_state') {
    process.stdout.write(JSON.stringify({ type: 'response', id: cmd.id, command: 'get_state', success: true }) + '\\n')
  } else if (cmd.type === 'prompt') {
    process.stdout.write(JSON.stringify({ type: 'response', id: cmd.id, command: 'prompt', success: true }) + '\\n')
    setTimeout(() => {
      const text = '<<ANALYSIS>>{"warmth":0.4,"effort":0.5,"attention":0.6}<<END>> RPC 回复'
      process.stdout.write(JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text }] } }) + '\\n')
      process.stdout.write(JSON.stringify({ type: 'agent_settled' }) + '\\n')
    }, 20)
  }
})
`)

process.env.WECHAT_BRIDGE_AGENT_RUN = FAKE_AGENT
process.env.WECHAT_BRIDGE_AGENT_RPC = '1'
process.env.WECHAT_BRIDGE_DAEMON_PY = process.execPath
process.env.WECHAT_BRIDGE_DAEMON = FAKE_DAEMON
process.env.FAKE_AGENT_LOG = AGENT_LOG
process.env.FAKE_DAEMON_LOG = DAEMON_LOG

const { askAgent } = await import('../wechat-bridge/bridge.mjs')
const { AgentRpc } = await import('../wechat-bridge/agent-rpc.mjs')

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed += 1; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}\n${e.stack ?? e}`) }
  }
  console.log(`\ntest_bridge_askagent_rpc: ${passed}/${tests.length} passed`)
  if (passed !== tests.length) process.exit(1)
}

t('askAgent: RPC 可用 → 走 RPC 不 spawn（返回 text+analysis）', async () => {
  globalThis.__agentRpc = new AgentRpc({ bin: process.execPath, args: [FAKE_PI] })
  try {
    const { text, analysis } = await askAgent('哥哥在吗')
    assert(text.includes('RPC 回复'), `text 应来自 RPC: ${text}`)
    assert(analysis && analysis.warmth === 0.4, 'analysis 应来自 RPC')
    const spawns = await (await import('node:fs/promises')).readFile(AGENT_LOG, 'utf8').catch(() => '')
    assert(spawns === '', `不应 spawn agent-run，实际: ${spawns}`)
  } finally {
    globalThis.__agentRpc.dispose()  // 清理 fake pi 子进程，避免事件循环挂起
    globalThis.__agentRpc = null
  }
})

t('askAgent: RPC 抛错 → 回退 spawn', async () => {
  globalThis.__agentRpc = { prompt: async () => { throw new Error('RPC down') } }
  const { text } = await askAgent('哥哥在吗')
  assert(text === 'spawn 回复', `应回退 spawn: ${text}`)
  const spawns = await (await import('node:fs/promises')).readFile(AGENT_LOG, 'utf8').catch(() => '')
  assert(spawns.includes('--analysis-mode'), '回退后应 spawn agent-run --analysis-mode')
})

await runAll()
