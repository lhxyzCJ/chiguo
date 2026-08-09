#!/usr/bin/env node
/**
 * tests/test_agent_rpc.mjs — AgentRpc 契约测试（fake pi 注入 bin，不 spawn 真 pi）。
 * 覆盖：握手、prompt 往返（text+analysis）、崩溃自动重启、restart 语义。
 */
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
process.chdir(root)
process.env.AGENTRUN_TELEMETRY = '0'

const { AgentRpc } = await import('../wechat-bridge/agent-rpc.mjs')
const FAKE = join(root, 'tests', 'fake-agent-rpc.mjs')

function assert(cond, msg = 'assert failed') {
  if (!cond) throw new Error(msg)
}

async function withRpc(fn) {
  const rpc = new AgentRpc({ bin: process.execPath, args: [FAKE] })
  try {
    await fn(rpc)
  } finally {
    rpc.dispose()
  }
}

async function test_handshake_ok() {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    assert(rpc.proc && !rpc.dead, '握手后进程应存活')
  })
  console.log('  OK test_handshake_ok')
}

async function test_prompt_returns_text_and_analysis() {
  await withRpc(async (rpc) => {
    const r = await rpc.prompt('哥哥在吗')
    assert(r.text.includes('测试回复'), `text 应来自 message_end: ${r.text}`)
    assert(r.analysis && r.analysis.warmth === 0.5, 'analysis 应被解析')
  })
  console.log('  OK test_prompt_returns_text_and_analysis')
}

async function test_crash_auto_restart() {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const oldPid = rpc.proc.pid
    rpc.proc.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 300))
    const r = await rpc.prompt('崩溃后')
    assert(r.text.includes('测试回复'), '崩溃后下一轮应自动重启')
    assert(rpc.proc && rpc.proc.pid !== oldPid, '应为新进程')
  })
  console.log('  OK test_crash_auto_restart')
}

async function test_restart_kills_process() {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    rpc.restart()
    assert(rpc.dead === true, 'restart 后应标记死亡')
    assert(rpc.proc === null, 'restart 后进程句柄应清空')
  })
  console.log('  OK test_restart_kills_process')
}

const tests = [
  test_handshake_ok,
  test_prompt_returns_text_and_analysis,
  test_crash_auto_restart,
  test_restart_kills_process,
]
for (const t of tests) await t()
console.log(`\n${'='.repeat(40)}`)
console.log(`ALL ${tests.length} tests passed.`)
