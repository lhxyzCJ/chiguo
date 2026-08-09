#!/usr/bin/env node
/**
 * tests/test_bridge_agent_http.mjs — bridge /agent/prompt 端点契约测试。
 * 直接测导出的 handleAgentPrompt（res stub）+ 真实 HTTP 路由（自建 server 挂端点）。
 */
import assert from 'node:assert'
import { createServer } from 'node:http'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'bridge-agent-http-'))
const FAKE_PI = join(tmp, 'fake-pi.mjs')
process.env.AGENTRUN_TELEMETRY = '0'

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

const { handleAgentPrompt } = await import('../wechat-bridge/bridge.mjs')
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
