// test_bridge_askpi.mjs — bridge.mjs askPi / analysis / 特殊命令链路接线测试（独立 runner）
// 用法: node test_bridge_askpi.mjs（退出码 0=全过，1=有失败）
// 集成式：fake pi-run（canned JSON 响应）+ fake daemon（记录 argv + 真实 shape JSON），真实 execFile 链路。
import assert from 'node:assert'
import { writeFileSync, mkdtempSync, appendFileSync, cpSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'bridge-askpi-'))
const FAKE_PI = join(tmp, 'fake-pi-run.mjs')
const FAKE_DAEMON = join(tmp, 'fake-daemon.mjs')
const PI_LOG = join(tmp, 'pi.log')
const DAEMON_LOG = join(tmp, 'daemon.log')

// pi 假死记账隔离：真 pi_health.py 拷贝到 tmp（状态落 tmp，绝不写真实 pi_health.json）
const PH_SCRIPT = join(tmp, 'pi_health.py')
cpSync(new URL('../scripts/pi_health.py', import.meta.url).pathname, PH_SCRIPT)
process.env.WECHAT_BRIDGE_PI_HEALTH = PH_SCRIPT

writeFileSync(FAKE_PI, `
import { readFileSync, appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_PI_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
process.stdout.write(readFileSync(process.env.FAKE_PI_RESPONSE, 'utf8'))
`)
writeFileSync(FAKE_DAEMON, `
import { appendFileSync } from 'node:fs'
appendFileSync(process.env.FAKE_DAEMON_LOG, JSON.stringify(process.argv.slice(2)) + '\\n')
const args = process.argv.slice(2)
if (args[0] === '--anniversary') {
  const p = args[1].split(' ')
  if (p[0] === 'add') {
    process.stdout.write(JSON.stringify({ action: 'anniversary_added', ok: true, id: '5839c237dcef', name: p.slice(3).join(' '), date: p[2], type: p[1] }))
  } else if (p[0] === 'list') {
    process.stdout.write(JSON.stringify({ action: 'anniversary_list', anniversaries: [], count: 0 }))
  } else {
    process.stdout.write(JSON.stringify({ action: 'anniversary_removed', ok: true }))
  }
} else if (args[0] === '--break') {
  process.stdout.write(JSON.stringify({ action: 'break_set', manual_override: args[1] === 'on', message: 'ok' }))
} else if (args[0] === '--attention') {
  process.stdout.write(JSON.stringify({ action: 'attention', ok: false, reason: 'fake 无注意力块' }))
} else if (args[0] === '--schedule-recall') {
  const q = args[1] ?? ''
  process.stdout.write(JSON.stringify({ action: 'schedule_recall', ok: true, query: q,
    matches: q === '生日' ? [{ type: 'anniversary', label: '哥哥的生日' }] : [] }))
}
process.exit(Number(process.env.FAKE_DAEMON_EXIT ?? 0))
`)

process.env.WECHAT_BRIDGE_PI_RUN = FAKE_PI
process.env.WECHAT_BRIDGE_DAEMON_PY = process.execPath
process.env.WECHAT_BRIDGE_DAEMON = FAKE_DAEMON
process.env.FAKE_PI_LOG = PI_LOG
process.env.FAKE_DAEMON_LOG = DAEMON_LOG
const { askPi, recordUserMsg, upgradeAnalysis, handleMessage, TurnQueue, runWithRecall, runWithAttention } = await import('../wechat-bridge/bridge.mjs')

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

// ── 6b:recall 信号 + 回复侧 --attention 注入(独立 runner 同款 t() 风格)──
t('recall 信号路由:信号 → 第二趟 pi → 回答(mock analysis JSON)', async () => {
  // fake askPi 返回含 recall 信号的 analysis;断言第二趟 pi 收到事实注入
  const calls = []
  const fakeRun = { exec: async (bin, args, opts) => {
    const joined = args.join(' ')
    calls.push(joined)
    if (joined.includes('--analysis-mode')) {
      return { stdout: JSON.stringify({ type: 'message_end', message: { content: [{ type: 'text', text: '<<ANALYSIS>>{"warmth":0.5,"recall":"生日"}<<END>>回答' }] } }) }
    }
    if (joined.includes('--schedule-recall')) {
      return { stdout: JSON.stringify({ type: 'message_end', message: { content: [{ type: 'text', text: '<<RECALL>>{"ok":true,"matches":[{"type":"anniversary","label":"哥哥的生日"}]}<<END>>哥哥的生日是5月11日呀' }] } }) }
    }
    throw new Error('unexpected')
  } }
  const r = await runWithRecall('哥哥我生日是什么时候', fakeRun)
  assert.ok(r.includes('5月11日'), `第二趟按事实回答: ${r}`)
  const second = calls.find((c) => c.includes('--schedule-recall'))
  assert.ok(second, '第二趟调用')
  assert.ok(second.includes('--facts'), `事实走 --facts 通道: ${second}`)
  assert.ok(second.includes('哥哥的生日'), `--facts 含真实匹配: ${second}`)
  assert.ok(!second.includes('检索事实：'), 'prompt 不放事实')
})
t('recall 无匹配 → --facts 空数组、prompt 保留原文(事实只走 --facts 单通道)', async () => {
  const calls = []
  const fakeRun = { exec: async (bin, args, opts) => {
    calls.push(args.join(' '))
    if (args.join(' ').includes('--analysis-mode')) {
      return { stdout: JSON.stringify({ type: 'message_end', message: { content: [{ type: 'text', text: '<<ANALYSIS>>{"warmth":0.5,"recall":"查无此事"}<<END>>回答' }] } }) }
    }
    return { stdout: JSON.stringify({ type: 'message_end', message: { content: [{ type: 'text', text: '回答' }] } }) }
  } }
  await runWithRecall('查无此事', fakeRun)
  const second = calls.find((c) => c.includes('--schedule-recall'))
  assert.ok(second, '第二趟调用')
  const parts = second.split(' ')
  assert.strictEqual(parts[parts.indexOf('--prompt') + 1], '查无此事', 'prompt 不放事实')
  assert.strictEqual(parts[parts.indexOf('--facts') + 1], '[]', '无匹配 → --facts 空数组')
})
t('--attention 回复侧注入:取数失败跳过注入继续 askPi(降级)', async () => {
  // daemon --attention 返回 ok:false → askPi 仍执行(无 attention 块)
  const got = await runWithAttention(null, async () => ({ text: '自然回复' }))   // 注入失败
  assert.ok(got.includes('自然回复'), '降级为现状行为')
})

// ── onMessage 链路（handleMessage：detect→execute→reply 接线，bridge.mjs）──
const botStub = () => {
  const replies = []
  return {
    replies,
    reply: async (msg, text) => { replies.push(text) },
    sendTyping: async () => {},
    send: async () => {},
  }
}
const msg = (text) => ({ userId: 'owner@im.wechat', text })
const queue = new TurnQueue()

t('handleMessage: 特殊命令（记住X月X日）→ 不调 pi、daemon --anniversary add、真实 shape 确认文案', async () => {
  const db = dLines().length
  const pb = pLines().length
  const bot = botStub()
  const r = await handleMessage('记住5月11日是迟菓生日', msg('记住5月11日是迟菓生日'), bot, queue)
  assert.strictEqual(r, 'special')
  assert.strictEqual(pLines().length, pb, '特殊命令不应调 pi')
  assert.deepStrictEqual(JSON.parse(dLines()[db]), ['--user-msg', '记住5月11日是迟菓生日'])
  assert.deepStrictEqual(JSON.parse(dLines()[db + 1]), ['--anniversary', 'add anniversary 05-11 迟菓生日'])
  assert.deepStrictEqual(bot.replies, ['记住了！05-11——迟菓生日。……哼，才不会忘记。'])
})
t('handleMessage: 特殊命令（放假了）→ 不调 pi、daemon --break on、确认文案', async () => {
  const db = dLines().length
  const pb = pLines().length
  const bot = botStub()
  const r = await handleMessage('放假了', msg('放假了'), bot, queue)
  assert.strictEqual(r, 'special')
  assert.strictEqual(pLines().length, pb, '特殊命令不应调 pi')
  assert.deepStrictEqual(JSON.parse(dLines()[db]), ['--user-msg', '放假了'])
  assert.deepStrictEqual(JSON.parse(dLines()[db + 1]), ['--break', 'on'])
  assert.ok(bot.replies[0].includes('放假了'), bot.replies[0])
})
t('handleMessage: 普通消息 → 走 askPi（--prompt 原文 --analysis-mode）+ upgradeAnalysis + 回复', async () => {
  const db = dLines().length
  const pb = pLines().length
  setResponse({ ok: true, text: '今天天气不错呢', analysis: { warmth: 0.3, effort: 0.4 } })
  const bot = botStub()
  const r = await handleMessage('今天天气怎么样', msg('今天天气怎么样'), bot, queue)
  assert.strictEqual(r, 'pi')
  assert.strictEqual(pLines().length, pb + 1, 'pi 应被调一次')
  assert.deepStrictEqual(JSON.parse(pLines()[pb]), ['--prompt', '今天天气怎么样', '--analysis-mode'])
  assert.deepStrictEqual(JSON.parse(dLines()[db]), ['--user-msg', '今天天气怎么样'])
  assert.deepStrictEqual(JSON.parse(dLines()[db + 1]), ['--attention'], '6b:回复侧先取 --attention(失败降级继续 askPi)')
  assert.deepStrictEqual(JSON.parse(dLines()[db + 2]), ['--user-msg', '今天天气怎么样', '--analysis', JSON.stringify({ warmth: 0.3, effort: 0.4 })])
  assert.deepStrictEqual(bot.replies, ['今天天气不错呢'])
})
t('handleMessage: 空文本 → 不调 pi/daemon、不回复', async () => {
  const db = dLines().length
  const pb = pLines().length
  const bot = botStub()
  const r = await handleMessage('  ', msg('  '), bot, queue)
  assert.strictEqual(r, null)
  assert.strictEqual(pLines().length, pb)
  assert.strictEqual(dLines().length, db)
  assert.deepStrictEqual(bot.replies, [])
})

;(async () => {
  await runAll()
  console.log(`test_bridge_askpi: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1); })
