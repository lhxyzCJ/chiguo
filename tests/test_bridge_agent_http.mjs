#!/usr/bin/env node
/**
 * tests/test_bridge_agent_http.mjs — bridge /agent/prompt 端点契约测试。
 * 直接测导出的 handleAgentPrompt（res stub）+ 真实 HTTP 路由（自建 server 挂端点）。
 */
import assert from 'node:assert'
import { createServer } from 'node:http'
import { writeFileSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'bridge-agent-http-'))
const FAKE_PI = join(tmp, 'fake-pi.mjs')
process.env.AGENTRUN_TELEMETRY = '0'
// B2: 本文件测的是 RPC 可用路径 → 显式开启(否则 handleAgentPrompt 会 503 拒绝)
process.env.WECHAT_BRIDGE_AGENT_RPC = '1'
// R3: 干净 HOME 注入——AgentRpc 构造会 mkdir PID_DIR + _killStale() 扫描 ~/.pi/agent,
// 不隔离会杀掉线上 bridge 的常驻 rpc 子进程(homeDir() 在模块顶层求值,须在 import 前设置)。
const prevHome = process.env.HOME
const cleanHome = join(tmpdir(), `chiguo-bridge-agent-http-${process.pid}`)
process.env.HOME = cleanHome
process.on('exit', () => {
  if (prevHome === undefined) delete process.env.HOME
  else process.env.HOME = prevHome
  rmSync(cleanHome, { recursive: true, force: true })
})

// fake pi：analysis/send 模板 marker 回复
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
      const isSend = cmd.message.includes('主动消息决策结果')
      const text = isSend
        ? '<<ANALYSIS>>{"warmth":0.5}<<END>> http(send)回复'
        : '<<ANALYSIS>>{"warmth":0.5}<<END>> http(analysis)回复'
      process.stdout.write(JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text }] } }) + '\\n')
      process.stdout.write(JSON.stringify({ type: 'agent_settled' }) + '\\n')
    }, 20)
  }
})
`)

const { handleAgentPrompt, TurnQueue } = await import('../wechat-bridge/bridge.mjs')
const { AgentRpc } = await import('../wechat-bridge/agent-rpc.mjs')

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed += 1; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}\n${e.stack ?? e}`) }
  }
  console.log(`\ntest_bridge_agent_http: ${passed}/${tests.length} passed`)
  if (passed !== tests.length) process.exit(1)
}

function resStub() {
  const res = { status: 0, body: null }
  res.writeHead = (s) => { res.status = s }
  res.end = (b) => { res.body = JSON.parse(b) }
  return res
}

t('handleAgentPrompt: send 模式成功 → 200 {ok,text}', async () => {
  globalThis.__agentRpc = new AgentRpc({ bin: process.execPath, args: [FAKE_PI] })
  try {
    const res = resStub()
    await handleAgentPrompt({ text: '{"action":"send"}', mode: 'send' }, res)
    assert(res.status === 200, `status=${res.status}`)
    assert(res.body.ok === true && res.body.text.includes('http(send)回复'), JSON.stringify(res.body))
  } finally { globalThis.__agentRpc.dispose(); globalThis.__agentRpc = null }
})

t('handleAgentPrompt: send 模式每次轮换 send 会话（#223：移走旧文件）；analysis 不动', async () => {
  const fs2 = await import('node:fs')
  const path2 = await import('node:path')
  const { fileURLToPath } = await import('node:url')
  const { encodeSessionDir } = await import('../wechat-bridge/command-detect.mjs')
  const repoRoot = path2.join(path2.dirname(fileURLToPath(import.meta.url)), '..')
  const bridgeDir = path2.join(repoRoot, 'wechat-bridge')
  const sdir = path2.join(cleanHome, '.pi', 'agent', 'sessions', encodeSessionDir(bridgeDir))
  fs2.mkdirSync(sdir, { recursive: true })
  const sendFile = path2.join(sdir, '2099-01-01T00-00-00-000Z_chiguo-send.jsonl')
  fs2.writeFileSync(sendFile, 'x\n')
  const backups = path2.join(cleanHome, '.chiguo', 'session-backups')
  globalThis.__agentRpc = new AgentRpc({ bin: process.execPath, args: [FAKE_PI] })
  try {
    const res = resStub()
    await handleAgentPrompt({ text: '{"action":"send"}', mode: 'send' }, res)
    assert(res.status === 200, `status=${res.status}`)
    assert.ok(!fs2.existsSync(sendFile), '旧 send 会话文件已移走（每轮全新）')
    assert.ok(fs2.readdirSync(backups).some((f) => f.endsWith('-chiguo-send.jsonl')), 'send 备份存在')
    // analysis 模式不轮换 send 会话
    fs2.writeFileSync(sendFile, 'y\n')
    const res2 = resStub()
    await handleAgentPrompt({ text: 'hi', mode: 'analysis' }, res2)
    assert(res2.status === 200, `analysis status=${res2.status}`)
    assert.ok(fs2.existsSync(sendFile), 'analysis 模式不应动 send 文件')
  } finally {
    globalThis.__agentRpc.dispose(); globalThis.__agentRpc = null
  }
})

t('handleAgentPrompt: 缺 text → 400', async () => {
  const res = resStub()
  await handleAgentPrompt({ mode: 'send' }, res)
  assert(res.status === 400, `status=${res.status}`)
})

t('handleAgentPrompt: 非法 mode → 400', async () => {
  const res = resStub()
  await handleAgentPrompt({ text: 'x', mode: 'hack' }, res)
  assert(res.status === 400, `status=${res.status}`)
})

t('handleAgentPrompt: RPC 抛错 → 503', async () => {
  globalThis.__agentRpc = { prompt: async () => { throw new Error('RPC down') } }
  const res = resStub()
  await handleAgentPrompt({ text: 'x', mode: 'analysis' }, res)
  assert(res.status === 503, `status=${res.status}`)
  assert(res.body.ok === false && String(res.body.error).includes('RPC down'))
  globalThis.__agentRpc = null
})

t('handleAgentPrompt: 并发经共享 TurnQueue 串行化（R20,同会话不并发 turn）', async () => {
  const queue = new TurnQueue()
  const active = { n: 0, max: 0 }
  globalThis.__agentRpc = {
    prompt: async () => {
      active.n += 1
      active.max = Math.max(active.max, active.n)
      await new Promise((r) => setTimeout(r, 30))
      active.n -= 1
      return { text: 'r', analysis: null }
    },
  }
  try {
    await Promise.all([
      handleAgentPrompt({ text: 'a', mode: 'analysis' }, resStub(), queue),
      handleAgentPrompt({ text: 'b', mode: 'analysis' }, resStub(), queue),
    ])
    assert(active.max === 1, `并发 turn: max=${active.max}（应被队列串行化）`)
  } finally { globalThis.__agentRpc = null }
})

t('handleAgentPrompt: send 模式 queue 忙 → 快速失败 queue_busy（不排队等、不产生孤儿 send turn）', async () => {
  // F-A17-004/R10: 发送侧 RPC 的总预算必须与 tick 125s 对齐。当前方有一慢 turn
  // 占用共享 TurnQueue 时，send RPC 不能再无限排队等待（超过 curl 125s 会让
  // tick 先超时 → 无条件回退 spawn → 双 LLM 并行 + RPC 结果丢弃）。
  // 修复语义：派发时 queue 忙 → 在预算内快速失败 queue_busy，且被取消的 turn
  // 绝不执行（不留孤儿 LLM 卡在队列里）。
  const queue = new TurnQueue()
  let sendPromptRuns = 0
  const prevWait = process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS
  process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS = '30'   // 测试缩小等待预算
  globalThis.__agentRpc = {
    restart: async () => {},
    prompt: async ({ mode } = {}) => { if (mode === 'send') sendPromptRuns += 1; return { text: 'r', analysis: null } },
  }
  try {
    // 先占用队列 150ms（前方慢 turn），期间 send 请求应快速 queue_busy 退出
    const blocking = queue.run(() => new Promise((r) => setTimeout(r, 150)))
    const res = resStub()
    const t0 = Date.now()
    await handleAgentPrompt({ text: '{"action":"send"}', mode: 'send' }, res, queue)
    const elapsed = Date.now() - t0
    assert(res.status === 503, `应 503, 实得 ${res.status} body=${JSON.stringify(res.body)}`)
    assert(res.body.ok === false && String(res.body.error).includes('queue_busy'),
      `错误应含 queue_busy: ${JSON.stringify(res.body)}`)
    assert(elapsed < 150, `应快速失败（未等前方 turn 完成）: ${elapsed}ms`)
    await blocking
    await new Promise((r) => setTimeout(r, 20))
    assert(sendPromptRuns === 0, `被取消的 send turn 不应执行（sendPromptRuns=${sendPromptRuns}）`)
  } finally {
    globalThis.__agentRpc = null
    if (prevWait === undefined) delete process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS
    else process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS = prevWait
  }
})

t('HTTP 路由:POST /agent/prompt 经真实 server → 200 + 非回环 Host 拒绝', async () => {
  globalThis.__agentRpc = new AgentRpc({ bin: process.execPath, args: [FAKE_PI] })
  const server = createServer((req, res) => {
    if (req.method !== 'POST' || req.url !== '/agent/prompt') {
      res.writeHead(405); res.end(JSON.stringify({ ok: false })); return
    }
    let body = ''
    req.on('data', (c) => { body += c })
    req.on('end', () => handleAgentPrompt(JSON.parse(body || '{}'), res))
  })
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const port = server.address().port
  try {
    const ok = await fetch(`http://127.0.0.1:${port}/agent/prompt`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: 'hi', mode: 'analysis' }),
    })
    assert(ok.status === 200, `status=${ok.status}`)
    const j = await ok.json()
    assert(j.ok === true && j.text.includes('http(analysis)回复'), JSON.stringify(j))
  } finally {
    globalThis.__agentRpc.dispose(); globalThis.__agentRpc = null
    await new Promise((r) => server.close(r))
  }
})

await runAll()
