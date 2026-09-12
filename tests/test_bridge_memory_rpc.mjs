#!/usr/bin/env node
/**
 * tests/test_bridge_memory_rpc.mjs — 常驻只读记忆边车 MemoryRpc + agent.mjs 回退链路测试。
 * 仿 tests/test_agent_rpc.mjs + tests/fake-agent-rpc.mjs 模式：fake memory server
 * （经 bin/args 注入，不起真实 memory/server.py）测 MemoryRpc 握手/查询/超时/
 * restart；再测 getAttention/getMemories 边车失败时回退 spawn（沿用
 * test_bridge_askagent.mjs 手法：env WECHAT_BRIDGE_DAEMON_PY/WECHAT_BRIDGE_DAEMON
 * + fake daemon + 真实 execFile 链路，不 stub 函数）。
 *
 * 用法: node tests/test_bridge_memory_rpc.mjs（退出码 0=全过，1=有失败）
 * 约束：只新增测试文件，不改业务代码。
 */
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import { rmSync, mkdirSync, writeFileSync, readFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import assert from 'node:assert'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const tmp = join(tmpdir(), `chiguo-bridge-memory-rpc-${process.pid}`)
mkdirSync(tmp, { recursive: true })

// 干净 HOME 注入：memory-rpc 的 PID_DIR 在模块顶层经 homeDir() 求值（须在 import 前设置）。
// 复刻 CI 场景（runner HOME 无 ~/.pi/agent）→ 本地同样覆盖"目录不存在需自建"路径。
const prevHome = process.env.HOME
const cleanHome = join(tmp, 'home')
mkdirSync(cleanHome, { recursive: true })
process.env.HOME = cleanHome
process.on('exit', () => {
  if (prevHome === undefined) delete process.env.HOME
  else process.env.HOME = prevHome
  rmSync(tmp, { recursive: true, force: true })
})

// O2: 防写真实 logs/agent-run.log
process.env.AGENTRUN_TELEMETRY = '0'

// ── fake memory server（可配：经 FAKE_MEMORY_MODE 切换行为）──
// 协议（memory/server.py）：stdin 一行 {"id","cmd",...} → stdout 一行 {"id","ok",...}。
// mode=ok: 全 canned 响应；hang: 仅就绪 ping 回应、查询永不回应（查超时用）；
// silent: 全静默（启动超时用）；okfalse: 就绪 ping 正常、查询一律 ok:false（抛错用）。
// 就绪 ping 识别：id 以 'ready-' 开头（与 MemoryRpc.ensureStarted 同约定）。
const FAKE_SERVER = join(tmp, 'fake-memory-server.mjs')
writeFileSync(FAKE_SERVER, `
import readline from 'node:readline'
const mode = process.env.FAKE_MEMORY_MODE ?? 'ok'
const isReadyPing = (req) => req.cmd === 'ping' && String(req.id ?? '').startsWith('ready-')
const rl = readline.createInterface({ input: process.stdin })
rl.on('line', (line) => {
  let req
  try { req = JSON.parse(line) } catch { return }
  if (mode === 'silent') return
  if (mode === 'hang' && !isReadyPing(req)) return
  let resp
  if (isReadyPing(req)) {
    resp = { id: req.id, ok: true, action: 'ping' }
  } else if (mode === 'okfalse') {
    resp = { id: req.id, ok: false, reason: 'fake 拒绝' }
  } else if (req.cmd === 'ping') {
    resp = { id: req.id, ok: true, action: 'ping' }
  } else if (req.cmd === 'attention') {
    resp = { id: req.id, ok: true, action: 'attention',
      attention: { t1: [], t2: [], t3: {}, week_num: 29, today_exceptions: [] },
      emotion: { affection: 55 }, week_num: 29, today_exceptions: [] }
  } else if (req.cmd === 'memory_search') {
    resp = { id: req.id, ok: true, action: 'memory_search', query: req.query,
      count: 1, memories: [{ text: '哥哥喜欢咖啡', category: 'preference' }] }
  } else {
    resp = { id: req.id, ok: false, reason: \`未知 cmd: \${req.cmd}\` }
  }
  process.stdout.write(JSON.stringify(resp) + '\\n')
})
`)

// ── fake daemon（spawn 回退链路用：记录 argv + 真实 shape JSON）──
const FAKE_DAEMON = join(tmp, 'fake-daemon.mjs')
const DAEMON_LOG = join(tmp, 'daemon.log')
writeFileSync(FAKE_DAEMON, `
import { appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_DAEMON_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
if (process.env.FAKE_DAEMON_EXIT === '1') {
  process.stderr.write('fake daemon 失败\\n')
  process.exit(1)
}
const args = process.argv.slice(2)
if (args[0] === '--attention') {
  process.stdout.write(JSON.stringify({ action: 'attention', ok: true,
    attention: { t1: [], t2: [], t3: {}, week_num: 29, today_exceptions: [] },
    emotion: {}, week_num: 29, today_exceptions: [] }))
} else if (args[0] === '--memory-search') {
  process.stdout.write(JSON.stringify({ action: 'memory_search', ok: true,
    query: args[1], count: 0, memories: [] }))
} else {
  process.stdout.write(JSON.stringify({ action: 'recorded', ok: true }))
}
`)
process.env.WECHAT_BRIDGE_DAEMON_PY = process.execPath
process.env.WECHAT_BRIDGE_DAEMON = FAKE_DAEMON
process.env.FAKE_DAEMON_LOG = DAEMON_LOG
// B2 确定性:本文件只测边车+回退 spawn，不测 agent RPC 分支
delete process.env.WECHAT_BRIDGE_AGENT_RPC

// B3: 边车 opt-in 门（WECHAT_BRIDGE_MEMORY_RPC=1）须在 import agent.mjs 前置位，
// 否则 MEMORY_RPC_ENABLED 求值为 false，回退链路测试走 spawn 直通。
process.env.WECHAT_BRIDGE_MEMORY_RPC = '1'
const { MemoryRpc } = await import('../wechat-bridge/memory-rpc.mjs')
const { getAttention, getMemories } = await import('../wechat-bridge/agent.mjs')

const rpcArgs = () => ({ bin: process.execPath, args: [FAKE_SERVER] })
const dLines = () => {
  try { return readFileSync(DAEMON_LOG, 'utf8').trim().split('\n').filter(Boolean) } catch { return [] }
}
const clearDaemonLog = () => { try { writeFileSync(DAEMON_LOG, '') } catch {} }

/** 每个用例独立 rpc（构造+用后 await restart，保证无子进程残留挂起事件循环）。 */
async function withRpc(fn) {
  const rpc = new MemoryRpc(rpcArgs())
  try {
    await fn(rpc)
  } finally {
    await rpc.restart()
  }
}

/** 定时器加速（仅测试内调整，不动业务代码）：把超长超时映射为毫秒级，
 *  覆盖 query 30s 查询超时与 ensureStarted 10s 启动超时两条真实路径。 */
async function withFastTimers(fn) {
  const real = globalThis.setTimeout
  const fast = { 30000: 60, 10000: 60 }
  globalThis.setTimeout = (cb, ms, ...rest) => real(cb, fast[ms] ?? ms, ...rest)
  try {
    return await fn()
  } finally {
    globalThis.setTimeout = real
  }
}

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed += 1; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}\n${e.stack ?? e}`) }
  }
  console.log(`\ntest_bridge_memory_rpc: ${passed}/${tests.length} passed`)
  if (passed !== tests.length) process.exit(1)
}

// ── MemoryRpc 构造缺省（bin=DAEMON_PY，args=[memory/server.py]，不起进程）──
t('constructor 缺省 bin/args 指向 DAEMON_PY + memory/server.py', async () => {
  const { DAEMON_PY } = await import('../wechat-bridge/env.mjs')
  const rpc = new MemoryRpc()
  try {
    assert.strictEqual(rpc.bin, DAEMON_PY)
    assert.ok(rpc.args[0].endsWith(join('memory', 'server.py')), `args 应指向边车脚本: ${rpc.args}`)
  } finally {
    await rpc.restart()
  }
})

// ── 握手 + pidfile ──
t('握手成功：ensureStarted 后进程存活 + pidfile 落盘', async () => {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    assert.ok(rpc.proc && !rpc.dead, '握手后进程应存活')
    const pidFile = rpc._pidFile()
    assert.strictEqual(Number(readFileSync(pidFile, 'utf8').trim()), rpc.proc.pid, 'pidfile 应指向边车 pid')
  })
})

// ── 查询往返 ──
t('query ping：ok/action/id 回显', async () => {
  await withRpc(async (rpc) => {
    const r = await rpc.query('ping')
    assert.strictEqual(r.ok, true)
    assert.strictEqual(r.action, 'ping')
    assert.ok(typeof r.id === 'string' && r.id.length > 0, '响应应带 id')
  })
})

t('query attention：形状同 --attention（整行透传）', async () => {
  await withRpc(async (rpc) => {
    const r = await rpc.query('attention')
    assert.strictEqual(r.ok, true)
    assert.strictEqual(r.action, 'attention')
    for (const k of ['attention', 'emotion', 'week_num', 'today_exceptions']) {
      assert.ok(k in r, `attention 响应缺键: ${k}`)
    }
    assert.strictEqual(r.week_num, r.attention.week_num)
  })
})

t('query memory_search：形状同 --memory-search（query/count/memories）', async () => {
  await withRpc(async (rpc) => {
    const r = await rpc.query('memory_search', { query: '咖啡' })
    assert.strictEqual(r.ok, true)
    assert.strictEqual(r.action, 'memory_search')
    assert.strictEqual(r.query, '咖啡')
    assert.strictEqual(r.count, r.memories.length)
    assert.ok(r.memories[0].text.includes('咖啡'), `记忆应透传: ${JSON.stringify(r.memories)}`)
  })
})

t('query 未知 cmd：边车 ok:false → query 抛错（含 reason）', async () => {
  await withRpc(async (rpc) => {
    await assert.rejects(rpc.query('bogus_cmd'), /未知 cmd/, '未知 cmd 应抛错（调用方回退 spawn）')
  })
})

t('query 全局 ok:false（fake 拒绝）：query 抛错', async () => {
  process.env.FAKE_MEMORY_MODE = 'okfalse'
  try {
    await withRpc(async (rpc) => {
      await assert.rejects(rpc.query('memory_search', { query: 'x' }), /fake 拒绝/)
    })
  } finally {
    delete process.env.FAKE_MEMORY_MODE
  }
})

// ── 超时（加速定时器走真实 30s/10s 代码路径）──
t('query 超时：hang 边车 → reject 超时 + 自动 restart', async () => {
  process.env.FAKE_MEMORY_MODE = 'hang'
  try {
    await withRpc(async (rpc) => {
      await rpc.ensureStarted()  // 就绪 ping 仍回应，握手正常
      await withFastTimers(async () => {
        await assert.rejects(rpc.query('memory_search', { query: 'x' }), /查询超时/)
      })
      assert.strictEqual(rpc.dead, true, '超时后应 restart（标记死亡）')
      assert.strictEqual(rpc.proc, null, '超时后进程句柄应清空')
    })
  } finally {
    delete process.env.FAKE_MEMORY_MODE
  }
})

t('ensureStarted 超时：silent 边车 → reject 启动超时', async () => {
  process.env.FAKE_MEMORY_MODE = 'silent'
  try {
    const rpc = new MemoryRpc(rpcArgs())
    try {
      await withFastTimers(async () => {
        await assert.rejects(rpc.ensureStarted(), /启动超时/)
      })
      assert.strictEqual(rpc.dead, true)
    } finally {
      await rpc.restart()
    }
  } finally {
    delete process.env.FAKE_MEMORY_MODE
  }
})

// ── 崩溃与 restart ──
t('崩溃自动重启：SIGKILL 后下一轮 query 自动恢复（新 pid）', async () => {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const oldPid = rpc.proc.pid
    rpc.proc.kill('SIGKILL')
    await new Promise((r) => setTimeout(r, 300))
    const resp = await rpc.query('ping')
    assert.strictEqual(resp.ok, true, '崩溃后下一轮应自动重启')
    assert.ok(rpc.proc && rpc.proc.pid !== oldPid, '应为新进程')
  })
})

t('restart：标记死亡 + 句柄清空 + pidfile 清理', async () => {
  await withRpc(async (rpc) => {
    await rpc.ensureStarted()
    const pidFile = rpc._pidFile()
    assert.ok(readFileSync(pidFile, 'utf8').trim().length > 0, '重启前 pidfile 应存在')
    await rpc.restart()
    assert.strictEqual(rpc.dead, true, 'restart 后应标记死亡')
    assert.strictEqual(rpc.proc, null, 'restart 后进程句柄应清空')
    let gone = false
    try { readFileSync(pidFile, 'utf8'); } catch { gone = true; }
    assert.ok(gone, 'restart 后 pidfile 应清理')
  })
})

t('_isOurServer pid 复用防护：无关进程不杀、真边车才认', async () => {
  const rpc = new MemoryRpc(rpcArgs())
  // argv 含 memory/server.py 的进程才算自家边车（--eval 挂名，不执行边车代码）
  const innocent = spawn(process.execPath, ['-e', 'setInterval(()=>{},1000)'], { stdio: 'ignore' })
  const ours = spawn(process.execPath, ['--eval', 'setInterval(()=>{},1000)', 'memory/server.py'], { stdio: 'ignore' })
  await new Promise((r) => setTimeout(r, 300))
  try {
    assert.strictEqual(rpc._isOurServer(innocent.pid), false, '无关进程不应判定为边车')
    assert.strictEqual(rpc._isOurServer(ours.pid), true, 'cmdline 含 memory/server.py 应判定为边车')
    assert.strictEqual(rpc._isOurServer(999999), false, '不存在 pid → false')
  } finally {
    await rpc.restart()
    try { innocent.kill('SIGKILL') } catch {}
    try { ours.kill('SIGKILL') } catch {}
  }
})

// ── agent.mjs 回退链路（globalThis.__memoryRpc 注入 + fake daemon 真实 execFile）──
t('getAttention：边车成功 → 直接返回，不 spawn', async () => {
  clearDaemonLog()
  globalThis.__memoryRpc = { query: async () => ({ ok: true, action: 'attention', week_num: 7 }) }
  try {
    const r = await getAttention()
    assert.deepStrictEqual(r, { ok: true, action: 'attention', week_num: 7 })
    assert.strictEqual(dLines().length, 0, '边车成功时不应 spawn daemon')
  } finally {
    delete globalThis.__memoryRpc
  }
})

t('getAttention：边车失败 → 回退 spawn（--attention 真实 shape）', async () => {
  clearDaemonLog()
  globalThis.__memoryRpc = { query: async () => { throw new Error('边车 down') } }
  try {
    const r = await getAttention()
    assert.strictEqual(r.ok, true)
    assert.strictEqual(r.action, 'attention')
    assert.ok('week_num' in r && 'today_exceptions' in r, `回退结果应为 --attention 形状: ${JSON.stringify(r)}`)
    assert.deepStrictEqual(JSON.parse(dLines()[0]), ['--attention'], '回退应 spawn daemon --attention')
  } finally {
    delete globalThis.__memoryRpc
  }
})

t('getMemories：边车成功 → 直接返回，不 spawn', async () => {
  clearDaemonLog()
  const canned = { ok: true, action: 'memory_search', query: '咖啡', count: 1, memories: [{ text: 'x' }] }
  globalThis.__memoryRpc = { query: async () => canned }
  try {
    const r = await getMemories('咖啡')
    assert.deepStrictEqual(r, canned)
    assert.strictEqual(dLines().length, 0, '边车成功时不应 spawn daemon')
  } finally {
    delete globalThis.__memoryRpc
  }
})

t('getMemories：边车失败 → 回退 spawn（--memory-search 查询串透传）', async () => {
  clearDaemonLog()
  globalThis.__memoryRpc = { query: async () => { throw new Error('边车 down') } }
  try {
    const r = await getMemories('咖啡')
    assert.strictEqual(r.ok, true)
    assert.strictEqual(r.action, 'memory_search')
    assert.strictEqual(r.query, '咖啡')
    assert.deepStrictEqual(JSON.parse(dLines()[0]), ['--memory-search', '咖啡'], '回退应 spawn daemon --memory-search 原文')
  } finally {
    delete globalThis.__memoryRpc
  }
})

t('getAttention/getMemories：边车失败 + spawn 失败 → null（软降级，不阻塞回复流）', async () => {
  globalThis.__memoryRpc = { query: async () => { throw new Error('边车 down') } }
  process.env.FAKE_DAEMON_EXIT = '1'
  try {
    assert.strictEqual(await getAttention(), null, 'spawn 失败应返回 null')
    assert.strictEqual(await getMemories('x'), null, 'spawn 失败应返回 null')
  } finally {
    delete globalThis.__memoryRpc
    delete process.env.FAKE_DAEMON_EXIT
  }
})

await runAll()
