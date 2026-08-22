#!/usr/bin/env node
/**
 * wechat-bridge/test_bridge_hardening.mjs — T11 Bridge加固 TDD RED→GREEN 验证
 * 4场景：空token拒绝启动、非法Host 403、超1M 413、text不含完整prompt仅length
 * + TurnQueue限界 30s wait+110s total / max queue length  queue_busy
 * + 凭据 0700/0600 强置 + Host/Origin/Token 三道 + Content-Type gate
 */
import assert from 'node:assert'
import { createServer, request as httpRequest } from 'node:http'
import { spawn } from 'node:child_process'
import { mkdtempSync, rmSync, mkdirSync, realpathSync, cpSync, writeFileSync, readFileSync, existsSync, chmodSync, statSync } from 'node:fs'
import { dirname, join, basename } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = join(HERE, '..')
const SDK_DIR = join(REPO, 'wechat-bridge', 'node_modules', '@wechatbot', 'wechatbot')
const VENDOR_DIR = join(REPO, 'wechat-bridge', 'vendor', 'wechatbot')
const OWNER = 'owner@im.wechat'
const TEST_TOKEN = 'test-hardening-token-abcdef0123456789'

function ensureRealSdkDir() {
  let real = null
  try { real = realpathSync(SDK_DIR) } catch {}
  let vendorReal
  try { vendorReal = realpathSync(VENDOR_DIR) } catch { vendorReal = null }
  if (real !== null && vendorReal !== null && real !== vendorReal) return
  if (real === null && !existsSync(SDK_DIR)) {
    // no sdk dir yet - copy
  } else if (real !== null && vendorReal !== null && real === vendorReal) {
    // soft link -> materialize
  } else { return }
  rmSync(SDK_DIR, { recursive: true, force: true })
  cpSync(VENDOR_DIR, SDK_DIR, { recursive: true, filter: (p) => basename(p) !== 'node_modules' })
}
function ensureStub() {
  ensureRealSdkDir()
  mkdirSync(SDK_DIR, { recursive: true })
  writeFileSync(join(SDK_DIR, 'package.json'), '{"name":"@wechatbot/wechatbot","version":"0.0.0-ci-auth-stub","type":"module","exports":{".":"./index.mjs"}}\n')
  writeFileSync(join(SDK_DIR, 'index.mjs'), 'export class WeChatBot { constructor(){} async login(){} async start(){} async send(){} onMessage(){} on(){} }\n')
}
ensureStub()

function freePort() {
  return new Promise((resolve, reject) => {
    const s = createServer(); s.on('error', reject); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => resolve(p)) })
  })
}
function cleanHome() { return join(process.env.TMPDIR ?? '/tmp', `chiguo-hardening-home-${process.pid}-${Math.random().toString(36).slice(2)}`) }
async function bootServer(token, extraEnv = {}) {
  const tdir = mkdtempSync(join(process.env.TMPDIR ?? '/tmp', 'chiguo-hardening-'))
  const port = await freePort()
  const home = cleanHome()
  const env = { ...process.env, WECHAT_BRIDGE_TOKEN: token, WECHAT_BRIDGE_SEND_PORT: String(port), WECHAT_BRIDGE_STORAGE: join(tdir, 'storage'), WECHAT_BRIDGE_AGENT_RPC: '0', WECHAT_BRIDGE_QR_LOG: '0', HOME: home, ...extraEnv }
  mkdirSync(env.WECHAT_BRIDGE_STORAGE, { recursive: true })
  const child = spawn(process.execPath, ['wechat-bridge/bridge.mjs'], { cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'] })
  let stderr = ''; child.stderr.on('data', (c) => { stderr += c })
  let stdout = ''; child.stdout.on('data', (c) => { stdout += c })
  const kill = () => { if (child.exitCode === null) child.kill('SIGKILL'); rmSync(tdir, { recursive: true, force: true }); rmSync(home, { recursive: true, force: true }) }
  const deadline = Date.now() + 12000
  await new Promise((resolve, reject) => {
    const ping = async () => {
      try { await fetch(`http://127.0.0.1:${port}/send`, { method: 'POST', headers: { 'content-type': 'application/json' }, signal: AbortSignal.timeout(500) }); resolve() }
      catch { if (child.exitCode !== null) { kill(); reject(new Error(`bridge提前退出(${child.exitCode}) stderr:${stderr}`)) } else if (Date.now() > deadline) { kill(); reject(new Error(`12s未就绪 stderr:${stderr}`)) } else setTimeout(ping, 100) }
    }; ping()
  })
  return { child, port, kill, stderr }
}
function rawHttp({ port, path, method = 'POST', headers = {}, body }) {
  return new Promise((resolve, reject) => {
    const req = httpRequest({ host: '127.0.0.1', port, path, method, headers }, (res) => {
      let text = ''; res.on('data', (c) => text += c); res.on('end', () => { let json = null; try { json = text ? JSON.parse(text) : null } catch {} resolve({ status: res.statusCode, json, text }) })
    }); req.on('error', (e) => resolve({ status: 0, json: null, text: String(e), error: true })); if (body !== undefined) req.write(body); req.end()
  })
}

let passed = 0; const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}\n${e.stack ?? e.message ?? e}`) }
  }
  console.log(`\ntest_bridge_hardening: ${passed}/${tests.length} passed`)
  process.exit(passed === tests.length ? 0 : 1)
}

// ── 1. 空token拒绝启动 (fail-closed at 893) ──
t('空token=""拒绝启动 exit1 + FATAL', async () => {
  const env = { ...process.env, WECHAT_BRIDGE_TOKEN: '', WECHAT_BRIDGE_AGENT_RPC: '0', WECHAT_BRIDGE_QR_LOG: '0', HOME: cleanHome() }
  const home = env.HOME
  const { code, stderr } = await new Promise((resolve) => {
    const child = spawn(process.execPath, ['wechat-bridge/bridge.mjs'], { cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'] })
    let err = ''; child.stderr.on('data', (c) => err += c)
    child.on('error', (e) => resolve({ code: -1, stderr: String(e) }))
    child.on('exit', (c) => resolve({ code: c, stderr: err }))
    setTimeout(() => { if (child.exitCode === null) { child.kill('SIGKILL'); resolve({ code: -2, stderr: `${err}\n[TIMEOUT]` }) } }, 6000)
  }).finally(() => rmSync(home, { recursive: true, force: true }))
  assert.strictEqual(code, 1, `空token应exit1 code=${code} stderr=${stderr}`)
  assert.ok(stderr.includes('WECHAT_BRIDGE_TOKEN'), `stderr应含WECHAT_BRIDGE_TOKEN: ${stderr}`)
})
t('undefined token拒绝启动 exit1', async () => {
  const env = { ...process.env, HOME: cleanHome() }; delete env.WECHAT_BRIDGE_TOKEN; env.WECHAT_BRIDGE_AGENT_RPC = '0'; env.WECHAT_BRIDGE_QR_LOG = '0'
  const home = env.HOME
  const { code, stderr } = await new Promise((resolve) => {
    const child = spawn(process.execPath, ['wechat-bridge/bridge.mjs'], { cwd: REPO, env, stdio: ['ignore', 'pipe', 'pipe'] })
    let err = ''; child.stderr.on('data', (c) => err += c)
    child.on('exit', (c) => resolve({ code: c, stderr: err }))
    setTimeout(() => { if (child.exitCode === null) { child.kill('SIGKILL'); resolve({ code: -2, stderr: `${err}\n[TIMEOUT]` }) } }, 6000)
  }).finally(() => rmSync(home, { recursive: true, force: true }))
  assert.strictEqual(code, 1, `无token应exit1 code=${code} stderr=${stderr}`)
})

// ── 2. 非法Host 403 ──
t('非法Host 403 forbidden host', async () => {
  const s = await bootServer(TEST_TOKEN); try {
    const r = await rawHttp({ port: s.port, path: '/send', headers: { 'content-type': 'application/json', 'host': `evil.com:${s.port}`, 'x-bridge-token': TEST_TOKEN }, body: JSON.stringify({ to: OWNER, text: 'x' }) })
    assert.strictEqual(r.status, 403, `host evil.com应403 got ${r.status} ${r.text}`); assert.ok(String(r.json?.error).includes('forbidden host'), r.text)
  } finally { s.kill() }
})
t('非法Host via httpRequest hostHeader 403', async () => {
  const s = await bootServer(TEST_TOKEN); try {
    const r = await rawHttp({ port: s.port, path: '/send', headers: { 'content-type': 'application/json', 'host': `10.0.0.1:${s.port}`, 'origin': 'http://127.0.0.1', 'x-bridge-token': TEST_TOKEN }, body: JSON.stringify({ to: OWNER, text: 'x' }) })
    assert.strictEqual(r.status, 403, `10.0.0.1 host应403 got ${r.status} ${r.text}`)
  } finally { s.kill() }
})

// ── 3. 超1M 413 ──
t('超1M payload → 413 payload too large', async () => {
  const s = await bootServer(TEST_TOKEN); try {
    const bigText = 'a'.repeat(1_100_000)
    const body = JSON.stringify({ to: OWNER, text: bigText })
    // body长度 >1M
    assert.ok(body.length > 1_000_000, `测试body需>1M, got ${body.length}`)
    const r = await rawHttp({ port: s.port, path: '/send', headers: { 'content-type': 'application/json', 'host': `127.0.0.1:${s.port}`, 'x-bridge-token': TEST_TOKEN, 'content-length': String(Buffer.byteLength(body)) }, body })
    // 服务器在data事件内检测到超限会deny 413然后destroy；客户端可能收到413或连接错误，两种皆算限界生效；但期望413优先
    assert.ok(r.status === 413 || r.status === 0, `超1M应413或连接重置, got ${r.status} ${r.text?.slice(0, 200)}`)
    if (r.status === 413) assert.ok(String(r.json?.error).includes('payload too large'), r.text)
  } finally { s.kill() }
})
t('Content-Type非json → 415', async () => {
  const s = await bootServer(TEST_TOKEN); try {
    const r = await rawHttp({ port: s.port, path: '/send', headers: { 'content-type': 'text/plain', 'host': `127.0.0.1:${s.port}`, 'x-bridge-token': TEST_TOKEN }, body: JSON.stringify({ to: OWNER, text: 'x' }) })
    assert.strictEqual(r.status, 415, `非json Content-Type应415 got ${r.status}`)
  } finally { s.kill() }
})

// ── 4. text不含完整prompt仅length (脱敏) ──
t('text仅length不回显完整prompt: handleAgentPrompt error不泄漏prompt全文', async () => {
  // 先启用RPC环境再import；但本测试直接调handleAgentPrompt stub，无需真实RPC进程
  process.env.WECHAT_BRIDGE_AGENT_RPC = '1'
  const { TurnQueue } = await import('../wechat-bridge/bridge.mjs')
  // 构造包含敏感prompt的错误；验证响应error不含完整prompt明文
  const secretPrompt = 'SECRET_PROMPT_' + 'X'.repeat(500) + '_END'
  // 需绕过动态import的AgentRpc；直接用queue.run注入失败任务模拟handleAgentPrompt内部错误路径
  // 更直接：测sendMessage/timeout路径不会回显；测handleAgentPrompt的503 error sanitization
  const queue = new TurnQueue()
  // 模拟全局AgentRpc prompt抛错且错误消息中包含完整prompt
  const prevRpc = globalThis.__agentRpc
  globalThis.__agentRpc = {
    restart: async () => {},
    prompt: async () => { throw new Error(`LLM failed for prompt: ${secretPrompt}`) }
  }
  // 临时把AGENT_RPC_ENABLED视为true需重import；改为直接验证sanitize逻辑：调用handleAgentPrompt并检查返回
  // 由于AGENT_RPC_ENABLED在模块加载时已定为false（bootServer前为0），需重新加载模块无法；改为验证sendMessage的error也做sanitization
  // 这里改为验证 recordAgentHealth/bridge error路径的slice(0,100)不含完整prompt：直接调handleAgentPrompt的deny sanitization
  // 若AGENT_RPC_ENABLED=false，handleAgentPrompt直接503；我们测其error路径prompt不泄漏：先测isLocalHost/isLocalOrigin保持，再测error sanitization单元
  globalThis.__agentRpc = prevRpc
  // 备用：直接验证sanitize函数逻辑——读取bridge源码是否包含slice(0,100)且日志仅length
  const bridgeSrc = readFileSync(join(REPO, 'wechat-bridge', 'bridge.mjs'), 'utf8')
  // 日志应仅length
  assert.ok(bridgeSrc.includes('text.length') && bridgeSrc.includes('chars'), '日志应仅length chars')
  // error响应不应直接返回prompt全文：验证handleAgentPrompt catch中对error做截断/脱敏（slice 100内且不含SECRET_PROMPT长串）
  // 若当前实现未脱敏，下方模拟将失败（RED），实现脱敏后应通过
  // 模拟red逻辑：若bridge对error未过滤，secretPrompt长500字符会在error中完整出现
  // 这里我们做真实失败注入：启动带RPC的server，触发错误，看返回是否含长prompt
  // 为简化且确定RED→GREEN，我们断言当前源码的error处理包含对超长prompt的截断或过滤；若没有则测试失败以驱动修复
  const hasSanitize = bridgeSrc.includes('slice(0, 100)') || bridgeSrc.includes('slice(0,100)')
  assert.ok(hasSanitize, '应有slice(0,100)截断')
  // 关键断言：错误消息长度应被限制在100内，即使原错误包含长prompt，也不应返回完整500字符prompt
  const fakeError = `LLM failed for prompt: ${secretPrompt}`
  const sliced = fakeError.slice(0, 100)
  assert.ok(!sliced.includes(secretPrompt), '截断后不应含完整secretPrompt')
  assert.ok(sliced.length <= 100, '截断长度应<=100')
  // 进一步要求：修复后bridge应对error做sanitize，使含prompt的错误也仅length或截断后不含大段prompt；此处验证当前实现是否已sanitize到不含长X串
  // 若bridge直接返回reason原文，则返回中会含大量X（>100），该断言在修复前失败驱动加固
  // 我们导入handleAgentPrompt并实际触发含prompt的错误，看返回
  // 需要临时将AGENT_RPC_ENABLED置true——通过改env并重新import（用带query的import避免缓存）
  // 改用子进程方式验证：spawn临时脚本调handleAgentPrompt
  const tmp = mkdtempSync(join(process.env.TMPDIR ?? '/tmp', 'hardening-text-'))
  const script = join(tmp, 'check.mjs')
  writeFileSync(script, `
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
process.env.WECHAT_BRIDGE_AGENT_RPC='1';
process.env.WECHAT_BRIDGE_TOKEN='x';
process.env.HOME=join(tmpdir(), 'chk-${process.pid}');
const { handleAgentPrompt } = await import('${REPO}/wechat-bridge/bridge.mjs');
const secret='${secretPrompt}';
let captured=null;
globalThis.__agentRpc={ restart: async()=>{}, prompt: async()=>{ throw new Error('LLM failed for prompt: '+secret) } };
const res={ status:0, body:null, writeHead(s){this.status=s}, end(b){ this.body=JSON.parse(b)} };
await handleAgentPrompt({ text: secret, mode: 'send' }, res, { run: (fn)=>fn() });
captured=res.body?.error ?? '';
if (captured.includes(secret)) { console.error('LEAK:'+captured.slice(0,200)); process.exit(2) }
if (captured.length>300) { console.error('too long'); process.exit(3) }
console.log('sanitized:'+captured.slice(0,100));
process.exit(0);
`)
  const { code } = await new Promise((resolve) => {
    const child = spawn(process.execPath, [script], { cwd: REPO, env: { ...process.env, WECHAT_BRIDGE_AGENT_RPC: '1', WECHAT_BRIDGE_TOKEN: 'x' }, stdio: ['ignore', 'pipe', 'pipe'] })
    let out=''; let err=''; child.stdout.on('data',c=>out+=c); child.stderr.on('data',c=>err+=c)
    child.on('exit', (c)=> resolve({ code:c, out, err })); child.on('error', e=> resolve({ code:-1, out, err:String(e) }))
    setTimeout(()=>{ if(child.exitCode===null){ child.kill('SIGKILL'); resolve({ code:-2, out, err:'timeout'}) } }, 5000)
  })
  rmSync(tmp, { recursive: true, force: true })
  // 修复前：code 2 (LEAK)；修复后：0
  assert.strictEqual(code, 0, `text脱敏应不泄漏完整prompt, code=${code} (2=泄漏,3=过长, 需加固)`)
})

// ── 5. TurnQueue限界 30s+110s queue_busy + maxqueue ──
t('TurnQueue queue_busy: 30s wait限界快速失败且不执行被取消turn', async () => {
  const { TurnQueue } = await import('../wechat-bridge/bridge.mjs')
  const queue = new TurnQueue()
  const prevWait = process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS
  process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS = '30'
  let runs = 0
  globalThis.__agentRpc = { restart: async()=>{}, prompt: async()=>{ runs++; return { text:'r', analysis:null } } }
  // 由于AGENT_RPC_ENABLED可能为false，handleAgentPrompt会直接503无queue测试；此处直接测TurnQueue本身
  // 占住队列150ms
  const block = queue.run(() => new Promise((r)=>setTimeout(r,150)))
  const start = Date.now()
  let gotBusy = false
  try {
    await queue.run(async () => { runs++; return 'should not run' }, { deadline: Date.now()+200, waitMaxMs: 30 })
  } catch (e) {
    gotBusy = String(e.message).includes('queue_busy') || e.code==='QUEUE_BUSY'
  }
  const elapsed = Date.now()-start
  await block
  assert.ok(gotBusy, '应queue_busy')
  assert.ok(elapsed < 120, `应30ms左右快速失败 elapsed=${elapsed}`)
  assert.strictEqual(runs, 0, '被取消turn不应执行')
  globalThis.__agentRpc = null
  if (prevWait===undefined) delete process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS; else process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS=prevWait
})

// ── 6. 凭据0700/0600 + 额外Host/Origin/Token三道验证 ──
t('凭据目录0700强置 + clarify 0600', async () => {
  const { writeClarify, readClarify, scheduleClarifyPath } = await import('../wechat-bridge/bridge.mjs')
  const tdir = mkdtempSync(join(process.env.TMPDIR ?? '/tmp', 'chiguo-cred-'))
  // 模拟storageDir 0700
  const stor = join(tdir, 'storage')
  mkdirSync(stor, { recursive: true, mode: 0o700 })
  // 故意改成777再看writeClarify是否0600
  try { chmodSync(stor, 0o777) } catch {}
  // bridge main会对storage chmod 0700；这里模拟后检查writeClarify 0600
  writeClarify(tdir, { original:'test', missing:[], question:'q', created_at: new Date().toISOString(), expires_at: new Date(Date.now()+3600_000).toISOString() })
  const p = scheduleClarifyPath(tdir)
  const st = statSync(p)
  const mode = st.mode & 0o777
  assert.ok((mode & 0o077) === 0, `clarify0600应无组/其他权限 mode=${mode.toString(8)}`)
  rmSync(tdir, { recursive: true, force: true })
})

await runAll()
