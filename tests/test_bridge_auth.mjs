#!/usr/bin/env node
/**
 * tests/test_bridge_auth.mjs — /send 与 /agent/prompt 端点鉴权测试（独立 runner，Q14）。
 *
 * 覆盖审计项（bridge.mjs）：
 *  - isLocalHost / isLocalOrigin（本地/非本地 origin 判断，纯函数，直接单元测试）
 *  - x-bridge-token（启动后共享 token 强制：缺失/错误/正确 三态）
 *  - :774 token 强制存在性（服务器启动时若无 WECHAT_BRIDGE_TOKEN 配置 → FATAL 拒绝启动）
 *
 * 鉴权执行路径（startSendServer 包装器，私有，未导出）经「真实子进程启动 bridge.mjs
 * + 真实 HTTP 请求」集成测试，不改动鉴权实现：子进程用 CI 替身 WeChatBot（@wechatbot/wechatbot，
 * node_modules，gitignored）满足 main() 走到 startSendServer 并保持存活。
 *
 * 用法：从仓库根运行 `node tests/test_bridge_auth.mjs`（退出码 0=全过，1=有失败）。
 * 隔离：临时 storage/家目录；每个带 token 场景独立子进程 + 独立端口；结束即 kill。
 */
import assert from 'node:assert'
import { createServer, request as httpRequest } from 'node:http'
import { spawn } from 'node:child_process'
import { writeFileSync, mkdtempSync, rmSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))            // <repo>/tests
const REPO = join(HERE, '..')                                  // 仓库根
const SDK_DIR = join(REPO, 'wechat-bridge', 'node_modules', '@wechatbot', 'wechatbot')
const SDK_PKG = join(SDK_DIR, 'package.json')
const SDK_ENTRY = join(SDK_DIR, 'index.mjs')
const OWNER = 'owner@im.wechat'
const TEST_TOKEN = 'test-bridge-token-0123456789abcdef'

// ── CI 替身 WeChatBot：满足 main() 走到 startSendServer（login/start/send/onMessage/on）。──
// node_modules 为 gitignored，不随资源提交；与 scripts/ci-test.sh 的替身同思路但补齐实例方法。
// 无条件覆盖写入完整 stub（而非检测文件存在即跳过）：ci-test.sh 顶部会用最小替身
// （class WeChatBot { constructor() {} } 无任何实例方法）自举写盘，若此处因文件存在而短路，
// 子进程 spawn 真实 bridge.mjs 时 bot.login()/bot.onMessage() 会抛 "not a function" 全挂。
function ensureStub() {
  mkdirSync(SDK_DIR, { recursive: true })
  writeFileSync(SDK_PKG,
    '{"name":"@wechatbot/wechatbot","version":"0.0.0-ci-auth-stub","type":"module","exports":{".":"./index.mjs"}}\n')
  writeFileSync(SDK_ENTRY, [
    'export class WeChatBot {',
    '  constructor() {}',
    '  async login() {}',
    '  async start() {}',
    '  async send() {}',
    '  onMessage() {}',
    '  on() {}',
    '}\n',
  ].join('\n'))
}

// ── 工具 ──
function freePort() {
  return new Promise((resolve, reject) => {
    const s = createServer()
    s.on('error', reject)
    s.listen(0, '127.0.0.1', () => {
      const p = s.address().port
      s.close(() => resolve(p))
    })
  })
}

function cleanHome() {
  return join(process.env.TMPDIR ?? '/tmp', `chiguo-auth-home-${process.pid}-${Math.random().toString(36).slice(2)}`)
}

let isLocalHost
let isLocalOrigin
let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}\n${e.stack ?? e}`) }
  }
  console.log(`\ntest_bridge_auth: ${passed}/${tests.length} passed`)
  process.exit(passed === tests.length ? 0 : 1)
}

// 启动真实 bridge 子进程（带 token → 起服务器跑 startSendServer）。
// 返回 { child, port, kill }；kill() 强制结束并清理。
async function bootServer(token) {
  const tdir = mkdtempSync(join(process.env.TMPDIR ?? '/tmp', 'chiguo-auth-'))
  const port = await freePort()
  const home = cleanHome()
  const env = {
    ...process.env,
    WECHAT_BRIDGE_TOKEN: token,
    WECHAT_BRIDGE_SEND_PORT: String(port),
    WECHAT_BRIDGE_STORAGE: join(tdir, 'storage'),
    WECHAT_BRIDGE_AGENT_RPC: '0',   // 关 RPC：仅测包装器鉴权；/agent/prompt 过鉴权后即 503
    WECHAT_BRIDGE_QR_LOG: '0',
    HOME: home,
  }
  mkdirSync(env.WECHAT_BRIDGE_STORAGE, { recursive: true })
  const child = spawn(process.execPath, ['wechat-bridge/bridge.mjs'], {
    cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'],
  })
  let stdout = ''
  let stderr = ''
  child.stdout.on('data', (c) => { stdout += c })
  child.stderr.on('data', (c) => { stderr += c })
  const deadline = Date.now() + 12_000
  const kill = () => {
    if (child.exitCode === null) child.kill('SIGKILL')
    rmSync(tdir, { recursive: true, force: true })
    rmSync(home, { recursive: true, force: true })
  }
  await new Promise((resolve, reject) => {
    const ping = async () => {
      try {
        await fetch(`http://127.0.0.1:${port}/send`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          signal: AbortSignal.timeout(500),
        })
        resolve()
      } catch {
        if (child.exitCode !== null) {
          kill(); reject(new Error(`bridge 子进程提前退出(${child.exitCode})\nstderr:${stderr}`))
        } else if (Date.now() > deadline) {
          kill(); reject(new Error(`server 12s 未就绪\nstderr:${stderr}`))
        } else setTimeout(ping, 100)
      }
    }
    ping()
  })
  return { child, port, kill, stdout, stderr }
}

// 向真实 bridge 服务器发一个断言请求
async function httpReq(port, path, { method = 'POST', headers = {}, body } = {}) {
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
    signal: AbortSignal.timeout(3000),
  })
  const text = await res.text()
  let json = null
  try { json = text ? JSON.parse(text) : null } catch { /* keep null */ }
  return { status: res.status, json, text }
}

const JSON_CT = 'application/json'

// 基于 node:http 的原始请求：可发出 fetch 会覆盖的 Host 头（Host 来自 URL 而非 header）。
// 用于验证"非本地 Host → 403 forbidden host"路径（fetch 强制 Host=URL 主机，无法注入）。
function rawHostReq(port, path, { hostHeader, origin, token, body } = {}) {
  const headers = { 'content-type': JSON_CT }
  if (origin !== undefined) headers.origin = origin
  if (token !== undefined) headers['x-bridge-token'] = token
  return new Promise((resolve, reject) => {
    const req = httpRequest({ host: '127.0.0.1', port, path, method: 'POST', headers: { ...headers, host: hostHeader } }, (res) => {
      let text = ''
      res.on('data', (c) => { text += c })
      res.on('end', () => {
        let json = null
        try { json = text ? JSON.parse(text) : null } catch { /* keep null */ }
        resolve({ status: res.statusCode, json, text })
      })
    })
    if (body !== undefined) req.write(JSON.stringify(body))
    req.end()
    req.on('error', reject)
  })
}

// ═════ 单元：isLocalHost / isLocalOrigin（审计零覆盖的导出助手）═════
t('isLocalHost: 本地回环 127.0.0.1/localhost/[::1]（含端口后缀）判真', () => {
  assert.ok(isLocalHost('127.0.0.1') === true)
  assert.ok(isLocalHost('127.0.0.1:18790') === true, '应容忍端口后缀')
  assert.ok(isLocalHost('localhost') === true)
  assert.ok(isLocalHost('LOCALHOST:9999') === true, '大小写不敏感')
  assert.ok(isLocalHost('[::1]') === true)
  assert.ok(isLocalHost('[::1]:18790') === true, '带括号的 IPv6 回环+HOST 端口')
  // 已知缺陷（回报 captain 再定）：裸「::1」（无方括号）会被端口剥离正则 /:\d+$/ 截成 ':',
  // 命中不了 '::1' 分支——但真实 Host 头对 IPv6 一律带方括号，故此断言锚定实际行为而非意图。
  assert.ok(isLocalHost('::1') === false, '裸 ::1 当前未被识别（IPv6 host 头需带方括号）')
})
t('isLocalHost: 非本地 host 判假（外部域名/局域网 IP/畸形/空）', () => {
  assert.ok(isLocalHost('example.com') === false)
  assert.ok(isLocalHost('192.168.1.1') === false)
  assert.ok(isLocalHost('10.0.0.5:18790') === false)
  assert.ok(isLocalHost('evil.com:18790') === false)
  assert.ok(isLocalHost('') === false)
  assert.ok(isLocalHost(undefined) === false)
  assert.ok(isLocalHost(null) === false)
})
t('isLocalOrigin: 无 Origin（curl 等）→ true（靠 Host+token 把关）', () => {
  assert.ok(isLocalOrigin(undefined) === true)
  assert.ok(isLocalOrigin(null) === true)
  assert.ok(isLocalOrigin('') === true)
})
t('isLocalOrigin: http://本地回环 → true', () => {
  assert.ok(isLocalOrigin('http://127.0.0.1:18790') === true)
  assert.ok(isLocalOrigin('http://localhost:18790') === true)
  assert.ok(isLocalOrigin('http://[::1]:18790') === true, 'IPv6 回环需方括号（URL 规范）')
})
t('isLocalOrigin: 非 http 协议 / 非本地 host / 畸形 → false', () => {
  assert.ok(isLocalOrigin('https://127.0.0.1:18790') === false, '仅 http 允许')
  assert.ok(isLocalOrigin('http://evil.com:18790') === false, '非本地 host 拒绝')
  assert.ok(isLocalOrigin('http://192.168.1.1') === false)
  assert.ok(isLocalOrigin('not-a-url') === false)
})

// ═════ 集成：真实服务器鉴权（带 token）═════
t('集成: 本地来源 + 正确 token → 200 {ok:true}', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const r = await httpReq(s.port, '/send', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}`, 'x-bridge-token': TEST_TOKEN },
      body: { to: OWNER, text: 'hello' },
    })
    assert.ok(r.status === 200, `status=${r.status} body=${r.text}`)
    assert.ok(r.json?.ok === true, `body=${r.text}`)
  } finally { s.kill() }
})
t('集成: 非本地 Origin → 403 forbidden origin', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const r = await httpReq(s.port, '/send', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: 'http://evil.com', 'x-bridge-token': TEST_TOKEN },
      body: { to: OWNER, text: 'x' },
    })
    assert.ok(r.status === 403, `status=${r.status}`)
    assert.ok(String(r.json?.error).includes('forbidden origin'), `body=${r.text}`)
  } finally { s.kill() }
})
t('集成: 非本地 Host → 403 forbidden host', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    // 无 Origin（curl 风格）→ isLocalOrigin 判真，交由 Host 把关
    // 用 node:http 原始请求注入 host 头（fetch 会强制 Host=URL 主机，测不到该分支）
    const r = await rawHostReq(s.port, '/send', {
      hostHeader: `evil.com:${s.port}`,
      origin: `http://127.0.0.1:${s.port}`,
      token: TEST_TOKEN,
      body: { to: OWNER, text: 'x' },
    })
    assert.ok(r.status === 403, `status=${r.status} body=${r.text}`)
    assert.ok(String(r.json?.error).includes('forbidden host'), `body=${r.text}`)
  } finally { s.kill() }
})
t('集成: 本地来源 + 错误 token → 403 forbidden token', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const r = await httpReq(s.port, '/send', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}`, 'x-bridge-token': 'wrong-token' },
      body: { to: OWNER, text: 'x' },
    })
    assert.ok(r.status === 403, `status=${r.status}`)
    assert.ok(String(r.json?.error).includes('forbidden token'), `body=${r.text}`)
  } finally { s.kill() }
})
t('集成: 本地来源 + 缺失 token 头 → 403 forbidden token', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const r = await httpReq(s.port, '/send', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}` },
      body: { to: OWNER, text: 'x' },
    })
    assert.ok(r.status === 403, `status=${r.status}`)
    assert.ok(String(r.json?.error).includes('forbidden token'), `body=${r.text}`)
  } finally { s.kill() }
})
t('集成: 非 application/json Content-Type → 415', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const r = await httpReq(s.port, '/send', {
      headers: { 'content-type': 'text/plain', host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}`, 'x-bridge-token': TEST_TOKEN },
    })
    assert.ok(r.status === 415, `status=${r.status}`)
  } finally { s.kill() }
})
t('集成: 正确 token 但非法收件人 → 403 forbidden recipient', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const r = await httpReq(s.port, '/send', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}`, 'x-bridge-token': TEST_TOKEN },
      body: { to: 'stranger@im.wechat', text: 'x' },
    })
    assert.ok(r.status === 403, `status=${r.status}`)
    assert.ok(String(r.json?.error).includes('forbidden recipient'), `body=${r.text}`)
  } finally { s.kill() }
})
t('集成: /agent/prompt 同享鉴权——错误 token → 403，正确 token → 通过鉴权(503 RPC 关闭)', async () => {
  const s = await bootServer(TEST_TOKEN)
  try {
    const bad = await httpReq(s.port, '/agent/prompt', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}`, 'x-bridge-token': 'wrong' },
      body: { text: 'hi', mode: 'analysis' },
    })
    assert.ok(bad.status === 403 && String(bad.json?.error).includes('forbidden token'), `bad status=${bad.status} body=${bad.text}`)
    const ok = await httpReq(s.port, '/agent/prompt', {
      headers: { 'content-type': JSON_CT, host: `127.0.0.1:${s.port}`, origin: `http://127.0.0.1:${s.port}`, 'x-bridge-token': TEST_TOKEN },
      body: { text: 'hi', mode: 'analysis' },
    })
    assert.ok(ok.status === 503, `鉴权应放行、随后 RPC 关闭 503; status=${ok.status} body=${ok.text}`)
  } finally { s.kill() }
})

// ═════ :774 token 强制存在性：无 token 配置 → FATAL 拒绝启动 ═════
t(':774 无 WECHAT_BRIDGE_TOKEN → 子进程 FATAL exit 1', async () => {
  const env = { ...process.env }
  delete env.WECHAT_BRIDGE_TOKEN
  env.WECHAT_BRIDGE_AGENT_RPC = '0'
  env.WECHAT_BRIDGE_QR_LOG = '0'
  env.HOME = cleanHome()
  const home = env.HOME
  const { code, stderr } = await new Promise((resolve) => {
    const child = spawn(process.execPath, ['wechat-bridge/bridge.mjs'], {
      cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let err = ''
    child.stderr.on('data', (c) => { err += c })
    child.on('error', (e) => resolve({ code: -1, stderr: String(e) }))
    child.on('exit', (c) => resolve({ code: c, stderr: err }))
    setTimeout(() => { if (child.exitCode === null) { child.kill('SIGKILL'); resolve({ code: -2, stderr: `${err}\n[TIMEOUT] 未退出` }) } }, 6000)
  }).finally(() => rmSync(home, { recursive: true, force: true }))
  assert.ok(code === 1, `exit code=${code}, stderr=${stderr}`)
  assert.ok(stderr.includes('[FATAL] WECHAT_BRIDGE_TOKEN 未设置'), `stderr=${stderr}`)
})

// 引导 CI 替身 stub（无条件覆盖为完整方法集）→ 动态 import bridge.mjs（取 isLocalHost/isLocalOrigin）
ensureStub()
const authFns = await import('../wechat-bridge/bridge.mjs')
isLocalHost = authFns.isLocalHost
isLocalOrigin = authFns.isLocalOrigin

await runAll()
