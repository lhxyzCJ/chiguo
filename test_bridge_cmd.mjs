// test_bridge_cmd.mjs — command-detect 特殊命令检测/执行测试（独立 runner）
// 用法: node test_bridge_cmd.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { detectSpecialCommand, inferYear, buildReply, executeSpecialCommand } from './wechat-bridge/command-detect.mjs'

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

// ── 检测：纪念日添加 ──
t('detect: 记住X月X日是XX → anniversary add MM-DD', () => {
  const r = detectSpecialCommand('记住5月11日是迟菓生日')
  assert.strictEqual(r.action, 'anniversary_added')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add anniversary 05-11 迟菓生日'])
})
t('detect: 记住X月X日XX（无"是"）→ 同样命中', () => {
  const r = detectSpecialCommand('记住3月1日开学日')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add anniversary 03-01 开学日'])
})
t('detect: 个位数月日补零', () => {
  const r = detectSpecialCommand('记住7月8日我们认识那天')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add anniversary 07-08 我们认识那天'])
})
t('detect: 尾标点剥离', () => {
  const r = detectSpecialCommand('记住11月3日主人生日！')
  assert.ok(!r.daemon[1].includes('！'), '感叹号不应进命令')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add anniversary 11-03 主人生日'])
})

// ── 检测：倒计时添加 ──
t('detect: YYYY年M月D日要XX → countdown YYYY-MM-DD', () => {
  const r = detectSpecialCommand('2026年12月25日要考试')
  assert.strictEqual(r.action, 'countdown_added')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add countdown 2026-12-25 考试'])
})
t('detect: M月D日要XX（无年份）→ 推断年份（已过→明年）', () => {
  const now = new Date(Date.now() + 8 * 3600 * 1000)
  const y = now.getUTCFullYear()
  const r = detectSpecialCommand(`5月11日要过生日`)
  const date = r.daemon[1].match(/add countdown (\d{4}-\d{2}-\d{2})/)[1]
  const year = Number(date.slice(0, 4))
  assert.ok(year === y || year === y + 1, `年份应为 ${y} 或 ${y + 1}，实得 ${year}`)
  if (Date.UTC(y, 4, 11) < Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) {
    assert.strictEqual(year, y + 1, '已过日期应推断到明年')
  }
})
t('inferYear: 边界——过去/未来日期', () => {
  const now = new Date(Date.now() + 8 * 3600 * 1000)
  const y = now.getUTCFullYear()
  const m = now.getUTCMonth() + 1
  const d = now.getUTCDate()
  // 昨天必已过 → 明年；构造"昨天"通用
  const past = new Date(Date.UTC(y, m - 1, d - 1) - 8 * 3600 * 1000)
  assert.strictEqual(inferYear(past.getUTCMonth() + 1, past.getUTCDate()), y + 1)
  const future = new Date(Date.UTC(y, m - 1, d + 1) - 8 * 3600 * 1000)
  assert.strictEqual(inferYear(future.getUTCMonth() + 1, future.getUTCDate()), y)
})

// ── 检测：列表 / 假期 ──
t('detect: 有哪些纪念日 → list', () => {
  const r = detectSpecialCommand('有哪些纪念日')
  assert.strictEqual(r.action, 'anniversary_list')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'list'])
})
t('detect: 纪念日列表 → list', () => {
  assert.strictEqual(detectSpecialCommand('纪念日列表').action, 'anniversary_list')
})
t('detect: 放假了 → break on', () => {
  const r = detectSpecialCommand('放假了')
  assert.strictEqual(r.action, 'break_on')
  assert.deepStrictEqual(r.daemon, ['--break', 'on'])
})
t('detect: 放暑假了/我放假了 → break on', () => {
  assert.strictEqual(detectSpecialCommand('放暑假了').action, 'break_on')
  assert.strictEqual(detectSpecialCommand('我放假了').action, 'break_on')
})
t('detect: 开学了 → break off', () => {
  const r = detectSpecialCommand('开学了')
  assert.strictEqual(r.action, 'break_off')
  assert.deepStrictEqual(r.daemon, ['--break', 'off'])
})

// ── 检测：不误伤（歧义/问句/长文/对话式）──
t('detect: 问句不拦截（"记住了吗"）', () => {
  assert.strictEqual(detectSpecialCommand('你记住我5月11日生日了吗'), null)
})
t('detect: 问句不拦截（"放假了吗"）', () => {
  assert.strictEqual(detectSpecialCommand('放假了吗'), null)
})
t('detect: 对bot提问不拦截（"你是几号开学"）', () => {
  assert.strictEqual(detectSpecialCommand('你是几号开学'), null)
})
t('detect: 长消息（>40字）不拦截（聊天场景）', () => {
  assert.strictEqual(detectSpecialCommand('今天天气真好啊，我们周末要不要一起出去走走，顺便纪念一下我们认识的日子'), null)
})
t('detect: 普通闲聊不拦截', () => {
  assert.strictEqual(detectSpecialCommand('今天好累啊'), null)
  assert.strictEqual(detectSpecialCommand('在忙吗'), null)
  assert.strictEqual(detectSpecialCommand('哥哥吃了吗'), null)
})
t('detect: 非字符串/空 → null', () => {
  assert.strictEqual(detectSpecialCommand(''), null)
  assert.strictEqual(detectSpecialCommand(null), null)
  assert.strictEqual(detectSpecialCommand(undefined), null)
})
t('detect: "今天放假了"（一天性陈述）不拦截 → 交 pi 自然回复', () => {
  assert.strictEqual(detectSpecialCommand('今天放假了'), null)
})

// ── buildReply：daemon JSON → 迟菓风文案 ──
t('buildReply: anniversary_added 成功', () => {
  assert.strictEqual(buildReply('anniversary_added', { action: 'anniversary_added', ok: true, id: 'a1', name: '迟菓生日', date: '05-11', type: 'anniversary' }),
    '记住了！05-11——迟菓生日。……哼，才不会忘记。')
})
t('buildReply: countdown_added 成功', () => {
  assert.strictEqual(buildReply('countdown_added', { action: 'anniversary_added', ok: true, name: '考试', date: '2026-12-25' }),
    '嗯嗯，考试（2026-12-25）——我算着日子呢。')
})
t('buildReply: list 非空（含倒计时标记）', () => {
  const r = buildReply('anniversary_list', { anniversaries: [
    { name: '考试', date: '2026-12-25', type: 'countdown' },
    { name: '生日', date: '05-11', type: 'anniversary' },
  ], count: 2 })
  assert.ok(r.includes('有 2 个'))
  assert.ok(r.includes('· 考试（2026-12-25 · 倒计时）'))
  assert.ok(r.includes('· 生日（05-11）'))
})
t('buildReply: list 空', () => {
  assert.ok(buildReply('anniversary_list', { anniversaries: [], count: 0 }).includes('一个都没有'))
})
t('buildReply: break on/off', () => {
  assert.ok(buildReply('break_on', { action: 'break_set', manual_override: true }).includes('放假了'))
  assert.ok(buildReply('break_off', { action: 'break_set', manual_override: false }).includes('开学了'))
})
t('buildReply: daemon error → 处理失败', () => {
  assert.ok(buildReply('anniversary_added', { ok: false, error: 'bad date' }).includes('处理失败：bad date'))
})
t('buildReply: daemon error（error 字段空）→ 兜底文案', () => {
  assert.ok(buildReply('anniversary_added', { ok: false }).includes('处理失败'))
})

// ── executeSpecialCommand：fake daemon 真实 execFile 链路 ──
const tmp = mkdtempSync(join(tmpdir(), 'bridge-cmd-'))
const FAKE_DAEMON = join(tmp, 'fake-daemon.mjs')
writeFileSync(FAKE_DAEMON, `
const args = process.argv.slice(2)
if (args[0] === '--break' && args[1] === 'on') {
  process.stdout.write(JSON.stringify({ action: 'break_set', manual_override: true, message: 'ok' }))
} else if (args[0] === '--anniversary' && args[1].startsWith('list')) {
  process.stdout.write(JSON.stringify({ action: 'anniversary_list', anniversaries: [{ name: '生日', date: '05-11', type: 'anniversary' }], count: 1 }))
} else if (args[0] === '--anniversary') {
  process.stdout.write(JSON.stringify({ action: 'anniversary_added', ok: true, name: '考试', date: '2026-12-25', type: 'countdown' }))
} else {
  process.stdout.write(JSON.stringify({ ok: false, error: 'boom' }))
  process.exit(1)
}
`)
import { spawn } from 'node:child_process'

t('executeSpecialCommand: break on → ok + 迟菓确认', async () => {
  const spec = detectSpecialCommand('放假了')
  const r = await executeSpecialCommand(spawn, spec, '/usr/bin/node', FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('放假了'))
})
t('executeSpecialCommand: anniversary add → ok + 名字日期', async () => {
  const spec = detectSpecialCommand('2026年12月25日要考试')
  const r = await executeSpecialCommand(spawn, spec, '/usr/bin/node', FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('考试') && r.reply.includes('2026-12-25'))
})
t('executeSpecialCommand: list → 行渲染', async () => {
  const spec = detectSpecialCommand('有哪些纪念日')
  const r = await executeSpecialCommand(spawn, spec, '/usr/bin/node', FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('· 生日（05-11）'))
})
t('executeSpecialCommand: daemon 非零退出 + error JSON → ok:false + 处理失败', async () => {
  const spec = { action: 'x', daemon: ['--bad'], hint: 'hint' }
  const r = await executeSpecialCommand(spawn, spec, '/usr/bin/node', FAKE_DAEMON)
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.includes('处理失败：boom'))
})
t('executeSpecialCommand: 脚本不存在 → ok:false（不抛未捕获异常）', async () => {
  const spec = detectSpecialCommand('放假了')
  const r = await executeSpecialCommand(spawn, spec, '/usr/bin/node', join(tmp, 'nope.mjs'))
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.startsWith('处理失败'))
})

;(async () => {
  await runAll()
  console.log(`test_bridge_cmd: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1); })
