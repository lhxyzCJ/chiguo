// test_bridge_cmd.mjs — command-detect 特殊命令检测/执行测试（独立 runner）
// 用法: node test_bridge_cmd.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { detectSpecialCommand, inferYear, buildReply, executeSpecialCommand, detectSlashCommand, executeSlashCommand, backupSessionFile, encodeSessionDir } from '../wechat-bridge/command-detect.mjs'

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
t('detect: 哥哥/主人 前缀兼容（哥哥记住X月X日是XX）', () => {
  const r = detectSpecialCommand('哥哥记住5月11日是迟菓生日')
  assert.strictEqual(r.action, 'anniversary_added')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add anniversary 05-11 迟菓生日'])
  assert.strictEqual(detectSpecialCommand('主人记住3月1日开学日').action, 'anniversary_added')
})
t('detect: 尾缀「了」不算名称（记住5月11日了 → 不拦截交 pi）', () => {
  assert.strictEqual(detectSpecialCommand('记住5月11日了'), null)
})
t('detect: 尾缀「了」剥离（记住5月11日是生日了 → 名称 生日）', () => {
  const r = detectSpecialCommand('记住5月11日是生日了')
  assert.deepStrictEqual(r.daemon, ['--anniversary', 'add anniversary 05-11 生日'])
})

// ── 检测：一次性提醒添加（6c:倒计时废弃 → reminder,经 --schedule-change）──
t('detect: YYYY年M月D日要XX → reminder_added + --schedule-change', () => {
  const r = detectSpecialCommand('2026年12月25日要考试')
  assert.strictEqual(r.action, 'reminder_added')
  assert.deepStrictEqual(r.daemon, ['--schedule-change', JSON.stringify({ kind: 'reminder', when: { date: '2026-12-25' }, label: '考试' })])
})
t('detect: M月D日要XX（无年份）→ 推断年份（已过→明年）', () => {
  const now = new Date(Date.now() + 8 * 3600 * 1000)
  const y = now.getUTCFullYear()
  const r = detectSpecialCommand(`5月11日要过生日`)
  assert.strictEqual(r.action, 'reminder_added')
  const item = JSON.parse(r.daemon[1])
  assert.strictEqual(item.kind, 'reminder')
  assert.strictEqual(item.label, '过生日')
  const date = item.when.date
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
t('detect: 列表两分支 ^ 锚定（"今天是纪念日列表"/"我们有哪些纪念日" 不命中）', () => {
  assert.strictEqual(detectSpecialCommand('今天是纪念日列表'), null)
  assert.strictEqual(detectSpecialCommand('我们有哪些纪念日'), null)
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
t('buildReply: reminder_added 成功（用 result.text 确认文案）', () => {
  assert.strictEqual(buildReply('reminder_added', { action: 'schedule_change', ok: true, text: '好,12月25日周日要考试,我记着。' }),
    '好,12月25日周日要考试,我记着。')
})
t('buildReply: reminder_added daemon 失败 → result.question', () => {
  assert.strictEqual(buildReply('reminder_added', { action: 'schedule_change', ok: false, reason: 'past_date', question: '这个日期已经过去了,告诉哥哥具体哪天的安排' }),
    '处理失败：这个日期已经过去了,告诉哥哥具体哪天的安排')
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
// fake daemon 输出 shape 固化自真实 daemon 实测（2026-08-02 隔离临时目录跑
// chiguo_daemon.py --anniversary add/list/remove + --break on）：
//   add   → {action:'anniversary_added', ok, id, name, date, type}          （chiguo_daemon.py:1186-1188）
//   list  → {action:'anniversary_list', anniversaries:[{id,type,name,date,note,created_at}...], count}  （:1195-1203）
//   remove→ {action:'anniversary_removed', ok}                               （:1191-1193）
//   break → {action:'break_set', manual_override, message}                   （:1318-1325）
const tmp = mkdtempSync(join(tmpdir(), 'bridge-cmd-'))
const FAKE_DAEMON = join(tmp, 'fake-daemon.mjs')
writeFileSync(FAKE_DAEMON, `
const args = process.argv.slice(2)
if (args[0] === '--break') {
  process.stdout.write(JSON.stringify({ action: 'break_set', manual_override: args[1] === 'on', message: '假期模式已' + (args[1] === 'on' ? '开启' : '关闭') }))
} else if (args[0] === '--anniversary' && args[1].startsWith('list')) {
  process.stdout.write(JSON.stringify({ action: 'anniversary_list', anniversaries: [
    { id: 'a1', type: 'countdown', name: '考试', date: '2026-12-25', note: '', created_at: '2026-08-01' },
    { id: 'a2', type: 'anniversary', name: '生日', date: '05-11', note: '', created_at: '2026-08-01' },
  ], count: 2 }))
} else if (args[0] === '--anniversary' && (args[1].startsWith('add countdown') || args[1].startsWith('add anniversary'))) {
  const p = args[1].split(' ')
  process.stdout.write(JSON.stringify({ action: 'anniversary_added', ok: true, id: '5839c237dcef', name: p.slice(3).join(' '), date: p[2], type: p[1] }))
} else if (args[0] === '--anniversary' && args[1].startsWith('remove')) {
  process.stdout.write(JSON.stringify({ action: 'anniversary_removed', ok: true }))
} else if (args[0] === '--anniversary' && args[1].startsWith('add')) {
  process.stdout.write(JSON.stringify({ action: 'anniversary_added', ok: false, error: 'month must be in 1..12' }))
  process.exit(1)
} else if (args[0] === '--schedule-change') {
  let item
  try { item = JSON.parse(args[1]) } catch {
    process.stdout.write(JSON.stringify({ action: 'schedule_change', ok: false, reason: 'bad_json', question: '处理失败,再试一次?' }))
    process.exit(1)
  }
  if (item.kind === 'remove') {
    process.stdout.write(JSON.stringify({ action: 'schedule_change', ok: false, reason: 'not_found', question: '没找到这条安排,哥哥再确认一下?', missing: ['period'] }))
    process.exit(1)
  }
  if (item.when && item.when.date === '2026-08-01') {
    process.stdout.write(JSON.stringify({ action: 'schedule_change', ok: false, reason: 'past_date', question: '这个日期已经过去了,告诉哥哥具体哪天的安排', missing: ['date'] }))
    process.exit(1)
  }
  if (item.kind === 'reminder') {
    process.stdout.write(JSON.stringify({ action: 'schedule_change', ok: true, text: \`好,\${item.label}(\${item.when.date})我记着。\` }))
    process.exit(0)
  }
  process.stdout.write(JSON.stringify({ action: 'schedule_change', ok: true, text: '好,8月20日周四要交材料,我记着。' }))
} else {
  process.stdout.write(JSON.stringify({ ok: false, error: 'boom' }))
  process.exit(1)
}
`)
import { spawn } from 'node:child_process'

t('executeSpecialCommand: break on → ok + 迟菓确认', async () => {
  const spec = detectSpecialCommand('放假了')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('放假了'))
})
t('executeSpecialCommand: reminder → ok + result.text 渲染（含 label+date）', async () => {
  const spec = detectSpecialCommand('2026年12月25日要考试')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('考试') && r.reply.includes('2026-12-25'), r.reply)
})
t('executeSpecialCommand: add anniversary（真实 shape）→ 记住了！MM-DD——名称', async () => {
  const spec = detectSpecialCommand('记住5月11日是迟菓生日')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('记住了！05-11——迟菓生日'), r.reply)
})
t('executeSpecialCommand: list → 真实 shape 行渲染（含倒计时标记 + count）', async () => {
  const spec = detectSpecialCommand('有哪些纪念日')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('有 2 个'), r.reply)
  assert.ok(r.reply.includes('· 生日（05-11）'), r.reply)
  assert.ok(r.reply.includes('· 考试（2026-12-25 · 倒计时）'), r.reply)
})
t('executeSpecialCommand: break off（真实 shape）→ 开学确认', async () => {
  const spec = detectSpecialCommand('开学了')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('开学了'), r.reply)
})
t('executeSpecialCommand: daemon 非零退出 + error JSON → ok:false + 处理失败', async () => {
  const spec = { action: 'x', daemon: ['--bad'], hint: 'hint' }
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.includes('处理失败：boom'))
})
t('executeSpecialCommand: 脚本不存在 → ok:false（不抛未捕获异常）', async () => {
  const spec = detectSpecialCommand('放假了')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, join(tmp, 'nope.mjs'))
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.startsWith('处理失败'))
})

// ── A4 形状契约:--schedule-change(批次 6a bridge 消费侧;二十轮点名 shape 与 test_schedule_cli.py 同源)──
t('executeSpecialCommand: --schedule-change 成功 shape(A4)→ ok:true(确认文案含星期+日期)', async () => {
  const spec = { action: 'schedule_change', daemon: ['--schedule-change', JSON.stringify({ kind: 'reminder', when: { date: '2026-08-20' }, label: '交材料' })], hint: 'x' }
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, true)
})
t('executeSpecialCommand: reminder_added past_date → ok:false + result.question 呈现', async () => {
  const spec = detectSpecialCommand('2026年8月1日要考试')
  assert.strictEqual(spec.action, 'reminder_added')
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.includes('处理失败'), r.reply)
  assert.ok(r.reply.includes('已经过去了'), r.reply)
})
t('executeSpecialCommand: --schedule-change 畸形 JSON 契约(A4 bad_json)→ ok:false + 处理失败兜底', async () => {
  const spec = { action: 'schedule_change', daemon: ['--schedule-change', '{not json'], hint: 'x' }
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.startsWith('处理失败'), r.reply)
})
t('executeSpecialCommand: --schedule-change ApiRejection shape(A4 reason+question+missing)→ ok:false', async () => {
  const spec = { action: 'schedule_change', daemon: ['--schedule-change', JSON.stringify({ kind: 'reminder', when: { date: '2026-08-01' }, label: '过去' })], hint: 'x' }
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, false)
})
t('executeSpecialCommand: --schedule-change remove 拒绝(not_found)→ ok:false 且失败 JSON 不丢 stdout', async () => {
  const spec = { action: 'schedule_change', daemon: ['--schedule-change', JSON.stringify({ kind: 'remove', match: { date: '2026-08-20' } })], hint: 'x' }
  const r = await executeSpecialCommand(spawn, spec, process.execPath, FAKE_DAEMON)
  assert.strictEqual(r.ok, false)
  assert.ok(r.reply.startsWith('处理失败'), r.reply)
})

;(async () => {
  await runAll()
  console.log(`test_bridge_cmd: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1); })
// ── 微信端斜杠命令 ──
t('slash: 白名单命中/未知拒绝/普通消息放行', () => {
  assert.strictEqual(detectSlashCommand('/new').action, 'new_session')
  assert.strictEqual(detectSlashCommand('/status').action, 'status')
  assert.strictEqual(detectSlashCommand('/记忆').action, 'memory_stats')
  assert.strictEqual(detectSlashCommand('/记得什么 火锅').action, 'memory_search')
  assert.strictEqual(detectSlashCommand('/记得什么 火锅').arg, '火锅')
  assert.strictEqual(detectSlashCommand('/help').action, 'help')
  assert.strictEqual(detectSlashCommand('/xyz').action, 'unknown_slash')
  assert.strictEqual(detectSlashCommand('你好呀'), null)
  assert.strictEqual(detectSlashCommand('记住5月11日是生日'), null)
})
t('slash: encodeSessionDir 与 pi 同编码(路径斜杠→横线,双横线包裹)', () => {
  assert.strictEqual(encodeSessionDir('/a/b/c'), '--a-b-c--')
})
t('slash: unknown_slash 迟菓风拒绝(不进 LLM)', async () => {
  const r = await executeSlashCommand(spawn, { action: 'unknown_slash' }, process.cwd())
  assert.strictEqual(r.ok, true)
  assert.ok(r.reply.includes('咒语'), r.reply)
})
t('slash: /help 列出白名单', async () => {
  const r = await executeSlashCommand(spawn, { action: 'help' }, process.cwd())
  assert.ok(r.reply.includes('/new') && r.reply.includes('/status') && r.reply.includes('/记忆'))
})
t('slash: /new 移走最近 chiguo-main 会话文件到备份目录', async () => {
  const fs = await import('node:fs')
  const os = await import('node:os')
  const path = await import('node:path')
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), 'slash-new-'))
  const dir = path.join(os.homedir(), '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
  fs.mkdirSync(dir, { recursive: true })
  const fake = path.join(dir, '2099-01-01T00-00-00-000Z_chiguo-main.jsonl')
  fs.writeFileSync(fake, '{"type":"session"}\n')
  const r = await executeSlashCommand(spawn, { action: 'new_session' }, cwd)
  assert.strictEqual(r.ok, true)
  assert.ok(!fs.existsSync(fake), '会话文件已移走')
  const backups = path.join(os.homedir(), '.chiguo', 'session-backups')
  assert.ok(fs.readdirSync(backups).some((f) => f.endsWith('-chiguo-main.jsonl')), '备份文件存在')
})
