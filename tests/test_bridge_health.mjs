// test_bridge_health.mjs — bridge 的 pi 假死记账 + 微信告警/恢复链路测试（独立 runner）
// 用法: node test_bridge_health.mjs（退出码 0=全过，1=有失败）
// 隔离: temp dir 内 fake pi-run（响应文件切换成败）+ 真 pi_health.py 拷贝（状态落 tmp）
// + stub bot（记录 reply/send）；绝不碰真实 pi_health.json。
import assert from 'node:assert'
import { writeFileSync, mkdtempSync, readFileSync, cpSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'bridge-health-'))
const FAKE_PI = join(tmp, 'fake-pi-run.mjs')
const FAKE_DAEMON = join(tmp, 'fake-daemon.py')
const PH_DIR = join(tmp, 'pi_health')
const PH_SCRIPT = join(PH_DIR, 'pi_health.py')
const PH_STATE = join(PH_DIR, 'pi_health.json')

writeFileSync(FAKE_PI, `
import { readFileSync } from 'node:fs'
process.stdout.write(readFileSync(process.env.FAKE_PI_RESPONSE, 'utf8'))
`)
writeFileSync(FAKE_DAEMON, 'import sys\nsys.exit(0)\n')
cpSync(new URL('../scripts/pi_health.py', import.meta.url).pathname, PH_SCRIPT)
const PH_REAL_CONTENT = readFileSync(PH_SCRIPT, 'utf8')

process.env.WECHAT_BRIDGE_PI_RUN = FAKE_PI
process.env.WECHAT_BRIDGE_DAEMON = FAKE_DAEMON
process.env.WECHAT_BRIDGE_PI_HEALTH = PH_SCRIPT

const { handleMessage, TurnQueue } = await import('../wechat-bridge/bridge.mjs')

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}: ${e.message}`); throw e }
  }
}

const sends = []
const replies = []
const bot = {
  reply: async (msg, text) => { replies.push(text) },
  sendTyping: async () => {},
  send: async (to, text) => { sends.push({ to, text }) },
}
const queue = new TurnQueue()

function setPiResponse(obj) {
  writeFileSync(join(tmp, 'resp.json'), JSON.stringify(obj))
  process.env.FAKE_PI_RESPONSE = join(tmp, 'resp.json')
}
const FAIL_RESP = { ok: false, error: '模拟 pi 故障' }
const OK_RESP = { ok: true, text: '正常回复', analysis: null }
const msg = { userId: 'owner@im.wechat' }

// ── 特殊命令路径不记账（不经 pi → pi_health 状态不变）──
t('特殊命令（纪念日）→ pi_health.json 内容不变（不记账）', async () => {
  setPiResponse(FAIL_RESP)
  const stateContent = () => {
    try { return readFileSync(PH_STATE, 'utf8') } catch { return '' }
  }
  const before = stateContent()
  await handleMessage('记住5月11日是生日', msg, bot, queue)
  assert.strictEqual(stateContent(), before, '特殊命令不得写入 pi_health')
  assert.strictEqual(sends.length, 0, '特殊命令不得触发告警')
})

// ── 假死告警：连续 3 次失败 → 恰 1 次告警；第 4 次不重复 ──
t('连续 3 次 askPi 失败 → 恰好 1 次告警（含次数与原因），第 4 次失败不重复告警', async () => {
  setPiResponse(FAIL_RESP)
  for (let i = 0; i < 3; i++) {
    await handleMessage('这是一条测试消息', msg, bot, queue)
  }
  assert.strictEqual(sends.length, 1, `期望 1 次告警，实际 ${sends.length}`)
  assert.ok(sends[0].text.includes('3'), `告警应含失败次数: ${sends[0].text}`)
  assert.ok(sends[0].text.includes('模拟 pi 故障'), `告警应含失败原因: ${sends[0].text}`)
  await handleMessage('这是一条新消息', msg, bot, queue)
  assert.strictEqual(sends.length, 1, `第 4 次失败不应重复告警，实际 ${sends.length}`)
})

// ── 恢复通知：成功后发恢复；再次成功不再发 ──
t('恢复后首次 success → 发送恢复通知；随后 success 不再发送', async () => {
  setPiResponse(OK_RESP)
  await handleMessage('我回来了呀今天', msg, bot, queue)
  assert.strictEqual(sends.length, 2, `期望 1 次恢复通知，实际 ${sends.length}`)
  assert.ok(sends[1].text.includes('恢复'), `应为恢复文案: ${sends[1].text}`)
  assert.ok(sends[1].to, '告警必须有收件人')
  await handleMessage('又一条新消息来了', msg, bot, queue)
  assert.strictEqual(sends.length, 2, '恢复后 success 不应再发送')
})

// ── 正常路径：无失败史时成功消息零告警 ──
t('健康状态下成功消息：零告警、零恢复', async () => {
  // 状态文件已在 up 状态，无需清理；直接再发成功消息
  await handleMessage('这是一条普通消息', msg, bot, queue)
  assert.strictEqual(sends.length, 2, `不应有新增发送，实际 ${sends.length}`)
})

// ── 记账失败（pi_health.py 崩溃）不阻塞回复流 ──
t('记账脚本崩溃 → 消息仍收到 ⚠️ 处理失败回复，进程不中断', async () => {
  const before = sends.length
  writeFileSync(PH_SCRIPT, 'raise SystemExit("boom")\n')
  try {
    setPiResponse(FAIL_RESP)
    await handleMessage('触发失败的消息来了', msg, bot, queue)
    assert.ok(replies.some((r) => r.startsWith('⚠️ 处理失败')), '应回复 ⚠️ 处理失败')
    assert.strictEqual(sends.length, before, '记账崩溃不应产生告警发送')
    // 恢复后链路仍可用
    writeFileSync(PH_SCRIPT, PH_REAL_CONTENT)
    setPiResponse(OK_RESP)
    await handleMessage('恢复链路的消息', msg, bot, queue)
    assert.ok(replies.some((r) => r === '正常回复'), '链路应恢复')
  } finally {
    writeFileSync(PH_SCRIPT, PH_REAL_CONTENT)
  }
})

// ── M1 回归：bot.reply 发送失败 ≠ pi 假死，不得误记/误告警 ──
t('bot.reply 失败（微信发送故障）→ 不误记 pi 假死：无告警、链路继续', async () => {
  const before = sends.length
  const origReply = bot.reply
  bot.reply = async () => { throw new Error('wechat 会话过期') }
  try {
    setPiResponse(OK_RESP)
    await handleMessage('发送失败的消息', msg, bot, queue)
  } finally {
    bot.reply = origReply
  }
  assert.strictEqual(sends.length, before, '回复失败不得触发告警/恢复（发送故障≠pi 故障）')
  // 记账仍记 success（pi 活着），且后续消息正常
  setPiResponse(OK_RESP)
  await handleMessage('下一条消息来了', msg, bot, queue)
  assert.ok(replies.at(-1) === '正常回复', '链路应继续正常回复')
})

await runAll()
console.log(`\n${passed} passed, ${tests.length - passed} failed`)
process.exit(passed === tests.length ? 0 : 1)
