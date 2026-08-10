#!/usr/bin/env node
/**
 * tests/test_agent_rpc.mjs — AgentRpc 契约测试（fake pi 注入 bin，不 spawn 真 pi）。
 * 覆盖：握手、prompt 往返（text+analysis）、崩溃自动重启、restart 语义。
 */
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import { rmSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { spawn } from 'node:child_process'

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

// C2: 专用 fake——stdout 按字节切分,cut 点落在多字节字符(测=0xE6 0xB5 0x8B)中间,
// 复现真实管道 chunk 边界任意性;若实现逐 chunk toString() 解码 → U+FFFD 损坏。
const CHUNKED_FAKE = join(cleanHome, 'chunked-fake.mjs')
mkdirSync(cleanHome, { recursive: true })
writeFileSync(CHUNKED_FAKE, `
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
      const ev = { type: 'message_end', message: { content: [{ type: 'text', text: '<<ANALYSIS>>{"warmth":0.5}<<END>> 测试中文🚀回复' }] } }
      const buf = Buffer.from(JSON.stringify(ev) + '\\n', 'utf8')
      const cut = buf.indexOf(0xE6) + 1   // 切在 '测' 首字节之后(0xE6 | 0xB5 0x8B),跨多字节字符
      process.stdout.write(buf.subarray(0, cut))
      process.stdout.write(buf.subarray(cut))
      const st = Buffer.from(JSON.stringify({ type: 'agent_settled' }) + '\\n', 'utf8')
      const m2 = Math.floor(st.length / 2)
      process.stdout.write(st.subarray(0, m2))
      process.stdout.write(st.subarray(m2))
    }, 20)
  }
})
`)

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

async function test_chunk_split_line_buffering() {
  // A1: stdout data 事件 chunk 边界任意——同一行被切成两段 emit,
  // 残段必须保留到下个 chunk,join 时不得在行中间插入换行(否则 agent_settled 永不 settle)。
  await withRpc(async (rpc) => {
    const key = rpc._key('analysis')
    const s = rpc._get(key)
    let settled = false
    s.pending = { resolve: () => { settled = true }, reject: () => {} }
    const line = JSON.stringify({ type: 'agent_settled' })
    const mid = Math.floor(line.length / 2)
    rpc._onChunk(key, line.slice(0, mid))
    assert(settled === false, '半个行不应触发 settle')
    assert(s.buffer.length === 0, '残段不应入 buffer')
    rpc._onChunk(key, line.slice(mid) + '\n')
    assert(settled, '跨 chunk 的完整行应被解析并 settle')
    assert(s.buffer.length === 1 && s.buffer[0] === line, 'buffer 应含完整行(无插入换行)')
  })
  console.log('  OK test_chunk_split_line_buffering')
}

async function test_multi_lines_single_chunk_and_residue() {
  // A1: 同 chunk 多行全解析;chunk 尾部残段保留跨 chunk 补齐
  await withRpc(async (rpc) => {
    const key = rpc._key('analysis')
    const s = rpc._get(key)
    rpc._onChunk(key, '{"a":1}\n{"b":2}\n{"c"')
    assert(JSON.stringify(s.buffer) === JSON.stringify(['{"a":1}', '{"b":2}']), '同 chunk 多行应全解析')
    assert(s.lineBuffer === '{"c"', '残段应保留待下 chunk')
    rpc._onChunk(key, ':3}\n')
    assert(s.buffer.length === 3, '残段补齐后完整入 buffer')
    assert(s.buffer[2] === '{"c":3}', '残段行应无换行污染')
    assert(s.lineBuffer === '', '消费后无残留')
  })
  console.log('  OK test_multi_lines_single_chunk_and_residue')
}

async function test_killstale_pid_reuse_guard() {
  // A2: pid 文件指向无关进程(pid 复用/误杀场景)不得杀;指向真 rpc 进程(--mode rpc)才清理。
  // 顺序注意: AgentRpc 构造时即 _killStale → 必须先建实例(无 pid 文件,无操作),再 spawn 验证判定,最后触发清理。
  const rpc = new AgentRpc({ bin: process.execPath, args: [FAKE] })
  const innocent = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' })
  const rpcProc = spawn(process.execPath, [FAKE, '--mode', 'rpc'], { stdio: ['pipe', 'pipe', 'pipe'] })
  await new Promise((r) => setTimeout(r, 300))
  assert(rpc._isOurRpcProcess(innocent.pid) === false, '无关进程不应判定为 rpc')
  assert(rpc._isOurRpcProcess(rpcProc.pid) === true, '真 rpc 进程应判定为 rpc')
  assert(rpc._isOurRpcProcess(999999) === false, '不存在 pid → false')
  const dir = join(cleanHome, '.pi', 'agent')
  mkdirSync(dir, { recursive: true })
  writeFileSync(join(dir, 'agent-rpc-innocent.pid'), String(innocent.pid))
  writeFileSync(join(dir, 'agent-rpc-real.pid'), String(rpcProc.pid))
  rpc._killStale()
  await new Promise((r) => setTimeout(r, 200))
  assert(innocent.exitCode === null, '非 rpc 进程不应被杀(pid 复用防护)')
  assert(rpcProc.signalCode === 'SIGTERM', '真 rpc 进程应被 _killStale 清理')
  rpc.dispose()
  try { innocent.kill('SIGKILL') } catch {}
  console.log('  OK test_killstale_pid_reuse_guard')
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

async function test_old_proc_exit_does_not_affect_new_session() {
  // C1: restart(超时/dispose)后旧进程 SIGTERM 延迟退出——旧 exit/error 回调不得
  // 标记新会话 dead、不得删新进程 pidfile、不得 reject 新回合 pending(回调须校验 proc 归属)。
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const key = rpc._key('analysis')
    const s = rpc._get(key)
    const oldProc = s.proc
    const oldPid = oldProc.pid
    rpc.restart()                       // 旧进程被 SIGTERM(延迟退出)
    await rpc.ensureStarted()           // 新进程已就绪,旧 exit 尚未到达
    const newProc = s.proc
    assert(newProc && newProc.pid !== oldPid, '应有新进程')
    assert(s.dead === false, '新会话存活')
    const pending = rpc.prompt('重启后测试')    // 新回合进行中
    oldProc.emit('error', new Error('old proc error'))
    oldProc.emit('exit', 0, null)              // 旧进程此刻退出
    const r = await pending
    assert(s.dead === false, '旧进程 exit 不得标记新会话 dead')
    assert(s.proc === newProc, '会话句柄仍为新进程')
    assert(r.text.includes('测试回复'), `新回合正常完成: ${r.text}`)
    const pidInFile = Number(readFileSync(rpc._pidFile(key), 'utf8').trim())
    assert(pidInFile === newProc.pid, `pidfile 仍指向新进程(旧 exit 不得删除): ${pidInFile}`)
  })
  console.log('  OK test_old_proc_exit_does_not_affect_new_session')
}

async function test_utf8_chunk_split_roundtrip() {
  // C2: stdout 逐 chunk 解码(toString)会把跨 chunk 的多字节字符截断成 U+FFFD;
  // setEncoding('utf8') 后 data 回调收到 string,多字节字符跨 chunk 无损拼接。
  const rpc = new AgentRpc({ bin: process.execPath, args: [CHUNKED_FAKE] })
  try {
    const r = await rpc.prompt('测试一下')
    assert(r.text.includes('测试中文🚀'), `跨 chunk 中文往返无损: ${r.text}`)
    assert(r.analysis && r.analysis.warmth === 0.5, 'analysis 跨 chunk 无损')
  } finally {
    rpc.dispose()
  }
  console.log('  OK test_utf8_chunk_split_roundtrip')
}

async function test_line_buffer_overflow_restarts_session() {
  // R1: 异常 pi 输出无换行巨型行 → lineBuffer 无上限增长 OOM;超限应丢弃残段并重启会话。
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const key = rpc._key('analysis')
    const s = rpc._get(key)
    const oldPid = s.proc.pid
    const big = 'x'.repeat(256 * 1024)
    let overflowed = false
    for (let i = 0; i < 8; i++) {
      rpc._onChunk(key, big)
      if (s.dead && s.lineBuffer.length === 0) { overflowed = true; break }
    }
    assert(overflowed, '超限后应丢弃残段并重启会话')
    assert(s.lineBuffer.length <= 1024 * 1024, `残段不得超过上限: ${s.lineBuffer.length}`)
    assert(s.proc === null && s.dead === true, '会话应被标记 dead 且进程句柄清空')
    // 溢出后可正常重启恢复
    await rpc.ensureStarted()
    assert(s.proc && s.proc.pid !== oldPid && !s.dead, '溢出后可重启恢复')
  })
  console.log('  OK test_line_buffer_overflow_restarts_session')
}

async function test_is_our_rpc_process_exact_argv() {
  // R2: 子串匹配过宽——'--model'(含 '--mode' 子串)或参数值含 'rpc' 不得误判;
  // 仅「独立 token '--mode' 且下一 token 恰为 'rpc'」认可。
  const rpc = new AgentRpc({ bin: process.execPath, args: [FAKE] })
  const real = spawn(process.execPath, [FAKE, '--mode', 'rpc'], { stdio: ['pipe', 'pipe', 'pipe'] })
  const model = spawn(process.execPath, [FAKE, '--model', 'xrpc-token'], { stdio: ['pipe', 'pipe', 'pipe'] })
  const value = spawn(process.execPath, [FAKE, '--config', 'rpc-mode'], { stdio: ['pipe', 'pipe', 'pipe'] })
  await new Promise((r) => setTimeout(r, 300))
  try {
    assert(rpc._isOurRpcProcess(real.pid) === true, '--mode rpc → true')
    assert(rpc._isOurRpcProcess(model.pid) === false,
      `'--model'(值含 rpc)不得误判——cmdline: ${readFileSync(`/proc/${model.pid}/cmdline`, 'utf8')}`)
    assert(rpc._isOurRpcProcess(value.pid) === false, '参数值含 rpc 不得误判')
    assert(rpc._isOurRpcProcess(999999) === false, '不存在 pid → false')
  } finally {
    rpc.dispose()
    for (const p of [real, model, value]) { try { p.kill('SIGKILL') } catch {} }
  }
  console.log('  OK test_is_our_rpc_process_exact_argv')
}

const tests = [
  test_handshake_ok,
  test_prompt_returns_text_and_analysis,
  test_crash_auto_restart,
  test_restart_kills_process,
  test_send_mode_uses_send_template,
  test_dual_session_isolation,
  test_restart_one_session_only,
  test_chunk_split_line_buffering,
  test_multi_lines_single_chunk_and_residue,
  test_killstale_pid_reuse_guard,
  test_old_proc_exit_does_not_affect_new_session,
  test_utf8_chunk_split_roundtrip,
  test_line_buffer_overflow_restarts_session,
  test_is_our_rpc_process_exact_argv,
]
for (const t of tests) await t()
console.log(`\n${'='.repeat(40)}`)
console.log(`ALL ${tests.length} tests passed.`)
