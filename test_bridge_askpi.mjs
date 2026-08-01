// test_bridge_askpi.mjs — bridge.mjs askPi / analysis 接线测试（独立 runner）
// 用法: node test_bridge_askpi.mjs（退出码 0=全过，1=有失败）
// 集成式：fake pi-run（canned JSON 响应）+ fake daemon（记录 argv），真实 execFile 链路。
import assert from 'node:assert'
import { writeFileSync, mkdtempSync, appendFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'bridge-askpi-'))
const FAKE_PI = join(tmp, 'fake-pi-run.mjs')
const FAKE_DAEMON = join(tmp, 'fake-daemon.mjs')
const PI_LOG = join(tmp, 'pi.log')
const DAEMON_LOG = join(tmp, 'daemon.log')

writeFileSync(FAKE_PI, `
import { readFileSync, appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_PI_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
process.stdout.write(readFileSync(process.env.FAKE_PI_RESPONSE, 'utf8'))
`)
writeFileSync(FAKE_DAEMON, `
import { appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_DAEMON_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
process.exit(Number(process.env.FAKE_DAEMON_EXIT ?? 0))
`)

process.env.WECHAT_BRIDGE_PI_RUN = FAKE_PI
process.env.WECHAT_BRIDGE_DAEMON_PY = '/usr/bin/node'
process.env.WECHAT_BRIDGE_DAEMON = FAKE_DAEMON
process.env.FAKE_PI_LOG = PI_LOG
process.env.FAKE_DAEMON_LOG = DAEMON_LOG
const { askPi, recordUserMsg, upgradeAnalysis } = await import('./wechat-bridge/bridge.mjs')

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

function setResponse(obj) {
  writeFileSync(join(tmp, 'resp.json'), typeof obj === 'string' ? obj : JSON.stringify(obj))
  process.env.FAKE_PI_RESPONSE = join(tmp, 'resp.json')
}
import { readFileSync } from 'node:fs'
const dLines = () => {
  try { return readFileSync(DAEMON_LOG, 'utf8').trim().split('\n').filter(Boolean) } catch { return [] }
}
const pLines = () => {
  try { return readFileSync(PI_LOG, 'utf8').trim().split('\n').filter(Boolean) } catch { return [] }
}

// ── askPi：调 pi-run（--prompt 原文 --analysis-mode）解析 {ok,text,analysis} ──
t('askPi: ok+analysis → {text, analysis} 且参数正确（--prompt 原文 --analysis-mode）', async () => {
  const before = pLines().length
  setResponse({ ok: true, text: '那、那也还行吧。', analysis: { warmth: 0.5, effort: 0.3 } })
  const r = await askPi('哥哥说了一句')
  assert.strictEqual(r.text, '那、那也还行吧。')
  assert.deepStrictEqual(r.analysis, { warmth: 0.5, effort: 0.3 })
  const line = JSON.parse(pLines()[before])
  assert.strictEqual(line[0], '--prompt')
  assert.strictEqual(line[1], '哥哥说了一句')
  assert.strictEqual(line[2], '--analysis-mode')
})
t('askPi: ok 无 analysis 块 → analysis=null', async () => {
  setResponse({ ok: true, text: '就普通一句话' })
  const r = await askPi('hi')
  assert.strictEqual(r.analysis, null)
  assert.strictEqual(r.text, '就普通一句话')
})
t('askPi: {ok:false,error} → reject 含 error', async () => {
  setResponse({ ok: false, error: 'pi exited 1: No API key found' })
  await assert.rejects(askPi('hi'), /No API key found/)
})
t('askPi: stdout 非 JSON → reject 提示 pi-run 输出非 JSON', async () => {
  setResponse('not json at all')
  await assert.rejects(askPi('hi'), /pi-run 输出非 JSON/)
})

// ── upgradeAnalysis：analysis → daemon --user-msg 原文 --analysis <JSON>（recv_dedup 升级）──
t('upgradeAnalysis: 对象 analysis → daemon --user-msg 原文 --analysis JSON', async () => {
  const before = dLines().length
  await upgradeAnalysis('原文消息', { warmth: -0.2, effort: 0.8, topic: '纪念日' })
  const line = JSON.parse(dLines()[before])
  assert.strictEqual(line[0], '--user-msg')
  assert.strictEqual(line[1], '原文消息')
  assert.strictEqual(line[2], '--analysis')
  assert.deepStrictEqual(JSON.parse(line[3]), { warmth: -0.2, effort: 0.8, topic: '纪念日' })
})
t('upgradeAnalysis: null analysis → 不调 daemon', async () => {
  const before = dLines().length
  await upgradeAnalysis('原文消息', null)
  assert.strictEqual(dLines().length, before, '不应有 daemon 调用')
})
t('upgradeAnalysis: daemon 失败 → 不抛错（不阻塞回复流）', async () => {
  process.env.FAKE_DAEMON_EXIT = '1'
  try {
    await upgradeAnalysis('x', { warmth: 0.1 })
  } finally {
    process.env.FAKE_DAEMON_EXIT = '0'
  }
})
t('recordUserMsg: 确定性记录不带 --analysis', async () => {
  const before = dLines().length
  await recordUserMsg('原始消息')
  const line = JSON.parse(dLines()[before])
  assert.strictEqual(line[0], '--user-msg')
  assert.strictEqual(line[1], '原始消息')
  assert.ok(!line.includes('--analysis'), '确定性记录不应带分析')
})
t('recordUserMsg: daemon 失败 → 不抛错', async () => {
  process.env.FAKE_DAEMON_EXIT = '1'
  try {
    await recordUserMsg('x')
  } finally {
    process.env.FAKE_DAEMON_EXIT = '0'
  }
})

// ── 全链路（对应 queue handler 顺序）：record → askPi → upgrade → 回复文本 ──
t('全链路: recordUserMsg → askPi → upgradeAnalysis 顺序与内容', async () => {
  const db = dLines().length
  const pb = pLines().length
  setResponse({ ok: true, text: '回复', analysis: { warmth: 0.9, effort: 0.1 } })
  await recordUserMsg('哥哥的消息')
  const { text: reply, analysis } = await askPi('哥哥的消息')
  await upgradeAnalysis('哥哥的消息', analysis)
  assert.strictEqual(reply, '回复')
  assert.deepStrictEqual(JSON.parse(dLines()[db]), ['--user-msg', '哥哥的消息'])
  assert.deepStrictEqual(JSON.parse(dLines()[db + 1]), ['--user-msg', '哥哥的消息', '--analysis', JSON.stringify({ warmth: 0.9, effort: 0.1 })])
  assert.strictEqual(pLines().length, pb + 1, 'pi 只被调一次')
})

;(async () => {
  await runAll()
  console.log(`test_bridge_askpi: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1); })
