#!/usr/bin/env node
/**
 * tests/test_bridge_send_timeout.mjs — F-A17-003: /send 超时不确定语义（独立 runner）。
 *
 * 覆盖审计项：
 *  - bridge 的 bot.send 超时 catch 分支必须返回带 timeout_uncertain 标记的响应体
 *    {"ok":false,...,"timeout_uncertain":true}（发起 /send 前超时=消息可能已送达，
 *    调用方必须与"明确失败"区别处理，不能当作确定失败触发 refund）。
 *  - 回归对照：明确失败（bot.send throw，非超时）响应体仅 ok:false + error，无
 *    timeout_uncertain；成功响应体 ok:true。
 *
 * sendMessage 为 startSendServer 的 /send 处理器提取出的纯函数（不触 res），
 * 超时值经 env WECHAT_BRIDGE_SEND_TIMEOUT_MS 可配，便于快速中断断言。
 * 隔离：纯内存 stub bot + 不联网，零触碰真实运行时。
 */
import assert from 'node:assert'

// 必须在动态 import bridge.mjs 之前设置（SEND_TIMEOUT_MS 在模块加载期读取）。
process.env.WECHAT_BRIDGE_SEND_TIMEOUT_MS = '80'
process.env.WECHAT_BRIDGE_OWNER = 'owner@im.wechat'

const { sendMessage } = await import('../wechat-bridge/bridge.mjs')

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}: ${e.message}`); throw e }
  }
}

// bot.send 永不 resolve → withTimeout 必然超时
function hangingBot() {
  return { send: () => new Promise(() => {}) }
}
// bot.send 抛明确错误（非超时）
function throwingBot() {
  return { send: async () => { throw new Error('context_token expired (prepare failed)') } }
}
// bot.send 正常 resolve
function okBot() {
  return { send: async () => {} }
}

// ── F-A17-003: 超时路径 ──
t('/send 超时 → 响应体带 timeout_uncertain 标记（ok:false，不当作确定失败）', async () => {
  const r = await sendMessage({ to: process.env.WECHAT_BRIDGE_OWNER, text: 'hello' }, hangingBot())
  assert.strictEqual(r.ok, false, `超时应 ok:false: ${JSON.stringify(r)}`)
  assert.strictEqual(r.timeout_uncertain, true, `超时应带 timeout_uncertain=true: ${JSON.stringify(r)}`)
  assert.ok(String(r.error).includes('timeout'), `error 应含 timeout: ${r.error}`)
  assert.strictEqual(r.status, 500, `超时应 500: ${r.status}`)
})

// ── 回归对照：明确失败 ──
t('/send 明确失败（非超时）→ 无 timeout_uncertain 标记', async () => {
  const r = await sendMessage({ to: process.env.WECHAT_BRIDGE_OWNER, text: 'x' }, throwingBot())
  assert.strictEqual(r.ok, false, `应 ok:false: ${JSON.stringify(r)}`)
  assert.ok(!r.timeout_uncertain, `明确失败不应带 timeout_uncertain: ${JSON.stringify(r)}`)
  assert.ok(String(r.error).includes('prepare failed'), `error 应透传原因: ${r.error}`)
})

// ── 回归对照：成功 ──
t('/send 成功 → ok:true 无超时标记', async () => {
  const r = await sendMessage({ to: process.env.WECHAT_BRIDGE_OWNER, text: 'y' }, okBot())
  assert.strictEqual(r.ok, true, `成功应 ok:true: ${JSON.stringify(r)}`)
  assert.ok(!r.timeout_uncertain, `成功不应带 timeout_uncertain: ${JSON.stringify(r)}`)
})

// ── 校验分支：非法收件人 / 空文本（保持既有语义，非本审计主线但顺带防回退）──
t('/send 非 owner 收件人 → 403 forbidden recipient', async () => {
  const r = await sendMessage({ to: 'stranger@im.wechat', text: 'x' }, okBot())
  assert.strictEqual(r.status, 403, `${r.status}`)
  assert.ok(String(r.error).includes('forbidden recipient'), `${r.error}`)
})
t('/send 空文本 → 400', async () => {
  const r = await sendMessage({ to: 'owner@im.wechat', text: '  ' }, okBot())
  assert.strictEqual(r.status, 400, `${r.status}`)
})

await runAll()
console.log(`\ntest_bridge_send_timeout: ${passed}/${tests.length} passed`)
process.exit(passed === tests.length ? 0 : 1)
