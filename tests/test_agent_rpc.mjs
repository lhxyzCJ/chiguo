#!/usr/bin/env node
/**
 * tests/test_agent_rpc.mjs — AgentRpc 契约测试（fake pi 注入 bin，不 spawn 真 pi）。
 * 覆盖：握手、prompt 往返（text+analysis）、崩溃自动重启、restart 语义。
 */
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import { rmSync } from 'node:fs'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
process.chdir(root)
process.env.AGENTRUN_TELEMETRY = '0'

// 干净 HOME 注入：agent-rpc 的 PID_DIR 在模块顶层经 homedir() 求值（须在 import 前设置）。
// 复刻 CI 场景（runner HOME 无 ~/.pi/agent）→ 本地同样覆盖"目录不存在需自建"路径（#108 回归防护）。
const prevHome = process.env.HOME
const cleanHome = join(tmpdir(), `chiguo-agent-rpc-test-${process.pid}`)
process.env.HOME = cleanHome
process.on('exit', () => {
  if (prevHome === undefined) delete process.env.HOME
  else process.env.HOME = prevHome
  rmSync(cleanHome, { recursive: true, force: true })
})

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
    const s = [...rpc.sessions.values()][0]
    assert(s && s.proc && !s.dead, '握手后进程应存活')
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
    const s0 = [...rpc.sessions.values()][0]
    const oldPid = s0.proc.pid
    s0.proc.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 300))
    const r = await rpc.prompt('崩溃后')
    assert(r.text.includes('测试回复'), '崩溃后下一轮应自动重启')
    const s1 = [...rpc.sessions.values()][0]
    assert(s1.proc && s1.proc.pid !== oldPid, '应为新进程')
  })
  console.log('  OK test_crash_auto_restart')
}

async function test_restart_kills_process() {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    rpc.restart()
    const s = [...rpc.sessions.values()][0]
    assert(s.dead === true, 'restart 后应标记死亡')
    assert(s.proc === null, 'restart 后进程句柄应清空')
  })
  console.log('  OK test_restart_kills_process')
}

async function test_send_mode_uses_send_template() {
  // send 模式：模板应为 buildSendPrompt（含"主动消息决策结果"），且独立进程
  await withRpc(async (rpc) => {
    const r = await rpc.prompt('{"action":"send","context":{}}', { mode: 'send' })
    assert(r.text.includes('测试回复(send模板)'), `send 模式应用 send 模板: ${r.text}`)
    assert(rpc.sessions.has('send|chiguo-send'), 'send 会话应注册')
  })
  console.log('  OK test_send_mode_uses_send_template')
}

async function test_dual_session_isolation() {
  // analysis 与 send 双会话：各自独立进程（pid 不同）、独立 pending 不冲突
  await withRpc(async (rpc) => {
    await rpc.ensureStarted({ mode: 'analysis' })
    await rpc.ensureStarted({ mode: 'send' })
    const keys = [...rpc.sessions.keys()]
    assert(keys.length === 2, `应有 2 个会话: ${keys}`)
    const a = rpc.sessions.get(keys.find((k) => k.startsWith('analysis')))
    const s = rpc.sessions.get(keys.find((k) => k.startsWith('send')))
    assert(a && s && a.proc.pid !== s.proc.pid, '双会话进程应隔离')
    // 并发 prompt 不串台
    const [ra, rs] = await Promise.all([
      rpc.prompt('分析消息', { mode: 'analysis' }),
      rpc.prompt('{"action":"send"}', { mode: 'send' }),
    ])
    assert(ra.text.includes('测试回复') && rs.text.includes('测试回复'))
  })
  console.log('  OK test_dual_session_isolation')
}

async function test_restart_one_session_only() {
  // restart({mode}) 只杀指定会话，另一会话进程存活
  await withRpc(async (rpc) => {
    await rpc.ensureStarted({ mode: 'analysis' })
    await rpc.ensureStarted({ mode: 'send' })
    const sendKey = [...rpc.sessions.keys()].find((k) => k.startsWith('send'))
    const sendPid = rpc.sessions.get(sendKey).proc.pid
    rpc.restart({ mode: 'send' })
    assert(rpc.sessions.get(sendKey).dead === true, 'send 会话应被杀')
    const a = [...rpc.sessions.keys()].find((k) => k.startsWith('analysis'))
    assert(rpc.sessions.get(a).proc && !rpc.sessions.get(a).dead, 'analysis 会话应存活')
    assert(sendPid !== undefined)
  })
  console.log('  OK test_restart_one_session_only')
}

const tests = [
  test_handshake_ok,
  test_prompt_returns_text_and_analysis,
  test_crash_auto_restart,
  test_restart_kills_process,
  test_send_mode_uses_send_template,
  test_dual_session_isolation,
  test_restart_one_session_only,
]
for (const t of tests) await t()
console.log(`\n${'='.repeat(40)}`)
console.log(`ALL ${tests.length} tests passed.`)
