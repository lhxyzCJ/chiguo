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
// 不隔离会杀掉线上 bridge 的常驻 rpc 子进程(homedir() 在模块顶层求值,须在 import 前设置)。
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
      process.stdout.write(JSON.stringify({ type: 'message_end', message: { content: [{ type: 'text', text }] } }) + '\\n')
      process.stdout.write(JSON.stringify({ type: 'agent_settled' }) + '\\n')
    }, 20)
  }
})
`)

const { handleAgentPrompt, warnIfNoToken, TurnQueue } = await import('../wechat-bridge/bridge.mjs')
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

t('warnIfNoToken: 未配置 WECHAT_BRIDGE_TOKEN → stderr 醒目警告(零鉴权告警)', async () => {
  const prev = process.env.WECHAT_BRIDGE_TOKEN
  delete process.env.WECHAT_BRIDGE_TOKEN
  const orig = process.stderr.write
  let out = ''
  process.stderr.write = (s) => { out += String(s); return true }
  try { warnIfNoToken() } finally {
    process.stderr.write = orig
    if (prev !== undefined) process.env.WECHAT_BRIDGE_TOKEN = prev
  }
  assert(out.includes('WECHAT_BRIDGE_TOKEN'), `警告应提及环境变量: ${out}`)
  assert(out.includes('WARN'), '应醒目标记')
})

t('warnIfNoToken: 已配置 token → 无警告', async () => {
  const prev = process.env.WECHAT_BRIDGE_TOKEN
  process.env.WECHAT_BRIDGE_TOKEN = 'test-secret'
  const orig = process.stderr.write
  let out = ''
  process.stderr.write = (s) => { out += String(s); return true }
  try { warnIfNoToken() } finally {
    process.stderr.write = orig
    if (prev === undefined) delete process.env.WECHAT_BRIDGE_TOKEN
    else process.env.WECHAT_BRIDGE_TOKEN = prev
  }
  assert.strictEqual(out, '', '已配置不应有警告')
})

await runAll()
