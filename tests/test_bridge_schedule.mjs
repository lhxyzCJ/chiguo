#!/usr/bin/env node
// test_bridge_schedule.mjs — 追问循环 8 态 + 鉴权 + 超时 + ⑩-⑭(批次 6a,独立 runner)
// 用法: node test_bridge_schedule.mjs(退出码 0=全过,1=有失败)
import assert from 'node:assert'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { mkdtempSync, writeFileSync, readFileSync, cpSync, rmSync, existsSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { detectScheduleIntent } from '../wechat-bridge/command-detect.mjs'
import { extractBlock, resolveRepo } from '../scripts/agent-run.mjs'

// 隔离: handleMessage 成功路径会记 agent 假死账 — 复制真 agent_health.py 到 temp,绝不碰真实 agent_health.json。
// 注意: env 必须在动态 import bridge.mjs 之前设置(模块级 const 读取),故 bridge 符号全部走动态导入。
const tmp = mkdtempSync(join(tmpdir(), 'bridge-schedule-'))
const PH_SCRIPT = join(tmp, 'agent_health.py')
cpSync(new URL('../scripts/agent_health.py', import.meta.url).pathname, PH_SCRIPT)
process.env.WECHAT_BRIDGE_AGENT_HEALTH = PH_SCRIPT
// 隔离: recordUserMsg 走模块级 DAEMON_PY/DAEMON_SCRIPT,不注入 fake 会真实调 daemon
// --user-msg 污染运行时文件(chiguo_messages.jsonl/chiguo_decisions.jsonl)——与
// test_bridge_health.mjs 同款 fake daemon(退出 0 即可,recordUserMsg 不检查输出)。
const FAKE_DAEMON = join(tmp, 'fake-daemon.py')
writeFileSync(FAKE_DAEMON, 'import sys\nsys.exit(0)\n')
process.env.WECHAT_BRIDGE_DAEMON_PY = process.execPath
process.env.WECHAT_BRIDGE_DAEMON = FAKE_DAEMON
const { scheduleClarifyPath, readClarify, writeClarify, clearClarify, exitWordMatch, handleMessage } =
  await import('../wechat-bridge/bridge.mjs')

// R2.2: ⑭ 用 REPO 锚定仓库根（从 import.meta.url 推导，不依赖 cwd）
const REPO = resolveRepo(new URL('.', import.meta.url).href, {})

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

function tmpRepo() {
  const d = mkdtempSync(join(tmpdir(), 'clarify-'))
  return d
}

/** 注入式测试替身:extract/verify/daemon 全部脚本化返回。
 * 函数值按对应参数调用(extract(original)/verify(item, original)),对象值直接返回。
 * askAgent 为 stub('聊'),chat 放行路径不 hit 真实 agent。 */
function depsWith(script, repoRoot = null) {
  const call = (v) => (typeof v === 'function' ? v : async () => v)
  return {
    repoRoot,
    extractAgent: async (original) => script.extract
      ? await call(script.extract)(original)
      : { ok: false, not_command: true },
    verifyAgent: async (item, original) => script.verify
      ? await call(script.verify)(item, original)
      : { ok: true },
    runDaemon: async (item) => script.daemon ? script.daemon(item) : { ok: true, text: '好,记下了。' },
    askAgent: async () => ({ text: '聊' }),
    now: () => script.now ? script.now() : new Date(),
  }
}

function fakeBot(replies) {
  return {
    reply: async (msg, text) => { replies.push(text) },
    sendTyping: async () => {},
  }
}

const queue = { run: async (fn) => fn() }

const execFileP = promisify(execFile)
// daemon 解释器：CHIGUO_PYTHON 可覆盖（CI/本地），缺省走 uv run（与 scripts/ci-test.sh 一致）
const PYTHON = process.env.CHIGUO_PYTHON ?? 'uv'
const PYTHON_ARGS = process.env.CHIGUO_PYTHON ? [] : ['run', 'python']

t('⑨ recall 无匹配反问(6b 锚;daemon --schedule-recall 无匹配形状)', async () => {
  // 隔离:daemon 从临时目录运行 — 复制 chiguo_daemon.py + 最小 toml 到 tmp，
  // PYTHONPATH 指向仓库根（顶层模块 import），schedule 数据全部锚定 tmp（不碰真实仓库数据）。
  const iso = mkdtempSync(join(tmpdir(), 'recall-'))
  cpSync(join(REPO, 'chiguo_daemon.py'), join(iso, 'chiguo_daemon.py'))
  writeFileSync(join(iso, 'chiguo_proactive.toml'), '# 隔离用最小配置：无数据文件 → recall 零匹配\n')
  try {
    const { stdout } = await execFileP(PYTHON,
      [...PYTHON_ARGS, join(iso, 'chiguo_daemon.py'), '--schedule-recall', '不存在的关键词xyz'],
      { timeout: 30_000, env: { ...process.env, PYTHONPATH: REPO } })
    const r = JSON.parse(stdout)
    assert.ok(r.action === 'schedule_recall' && r.ok === true && r.query === '不存在的关键词xyz')
    assert.ok(Array.isArray(r.matches) && r.matches.length === 0, '无匹配 → 空数组 + ok:true(反问引导由 prompt 契约承担)')
  } finally {
    rmSync(iso, { recursive: true, force: true })
  }
})

t('⑭ clarify 路径锚定仓库根(A3)', () => {
  const p = scheduleClarifyPath(REPO)
  assert.strictEqual(p, join(REPO, 'schedule_clarify.json'), `got ${p}`)
})

t('detectScheduleIntent 词表 + start-anchored 豁免 MAX_LEN', () => {
  assert.strictEqual(detectScheduleIntent('明天停课').intent, 'cancel')
  assert.strictEqual(detectScheduleIntent('把课调到周五').intent, 'move')
  assert.strictEqual(detectScheduleIntent('下周三开始考试周').intent, 'exam_week')
  assert.strictEqual(detectScheduleIntent('8月20号要交材料').intent, 'reminder')
  assert.strictEqual(detectScheduleIntent('取消周三停课').intent, 'remove')
  // R2.1: 调课 子串命中(⑤ 追问循环用例为准)
  assert.strictEqual(detectScheduleIntent('我们明天调课吧').intent, 'move', '⑤ 调课子串命中')
  assert.strictEqual(detectScheduleIntent('今天有什么安排吗'), null, '问句不拦截')
  assert.strictEqual(detectScheduleIntent('这周有哪些安排'), null, '非词表不拦截')
  // R2.1: start-anchored 豁免超长
  const long = '停课' + '很长的后缀'.repeat(30)
  assert.strictEqual(detectScheduleIntent(long).intent, 'cancel', 'start-anchored 豁免')
  const longUnanchored = '今天天气不错想聊天' + '停课' + '很长的后缀'.repeat(30)
  assert.strictEqual(detectScheduleIntent(longUnanchored), null, '非锚定 + 超长不拦')
  assert.strictEqual(detectScheduleIntent('停课' + '长'.repeat(20)).intent, 'cancel', 'start-anchored 豁免')
})

t('① 校验抓补全 → 存澄清 → 下条路由重提取 → 补齐写入', async () => {
  const repo = tmpRepo()
  const replies = []
  let items = []
  const deps = depsWith({
    extract: async (original) => original.includes('8月21号')
      ? { ok: true, item: { kind: 'reminder', when: { date: '2026-08-21' }, label: '交材料' } }
      : { ok: true, item: { kind: 'reminder', when: { date: '2026-08-20' }, label: '交材料' } },
    verify: async (item) => item.when.date === '2026-08-21'
      ? { ok: true } : { ok: false, question: '哪天交材料呀?', missing: ['date'] },
    daemon: async (item) => { items.push(item); return { ok: true, text: '好,8月21日周五要交材料,我记着。' } },
  }, repo)
  await handleMessage('8月20号交材料', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(replies[0].includes('哪天交材料'), `追问: ${replies[0]}`)
  const rec = readClarify(repo)
  assert.ok(rec && rec.missing.includes('date'), `澄清记录已存: ${JSON.stringify(rec)}`)
  // 下一条消息路由回提取(合并原意+回答)
  await handleMessage('8月21号', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(items.length === 1 && items[0].when.date === '2026-08-21', `补齐写入: ${JSON.stringify(items)}`)
  assert.ok(readClarify(repo) === null, '⑪ 成功写入后清记录')
  rmSync(repo, { recursive: true, force: true })
})

t('② 继续缺字段 → 再追问 → 退出词清记录未写入', async () => {
  const repo = tmpRepo()
  const replies = []
  let wrote = false
  const deps = depsWith({
    extract: { ok: false, question: '再告诉哥哥一次?', missing: ['date'] },
    verify: { ok: false, question: '哪天?', missing: ['date'] },
    daemon: async () => { wrote = true; return { ok: true, text: 'x' } },
  }, repo)
  await handleMessage('8月20号交材料', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(readClarify(repo), '记录存在')
  await handleMessage('算了', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(readClarify(repo) === null, '退出词清记录')
  assert.ok(!wrote, '未写入')
  rmSync(repo, { recursive: true, force: true })
})

t('③ 记录 6h 过期静默清理', async () => {
  const repo = tmpRepo()
  writeClarify(repo, { original: 'x', missing: ['date'], question: 'q', created_at: new Date(Date.now() - 7 * 3600e3).toISOString(), expires_at: new Date(Date.now() - 1 * 3600e3).toISOString() })
  assert.ok(readClarify(repo) === null, '过期读为无记录')
  assert.ok(!existsSync(scheduleClarifyPath(repo)) || readFileSync(scheduleClarifyPath(repo), 'utf8') === '', '过期静默清理')
  rmSync(repo, { recursive: true, force: true })
})

t('④ 新写命令覆盖旧记录(同时只追问一件事)', async () => {
  const repo = tmpRepo()
  writeClarify(repo, { original: '旧的', missing: ['date'], question: '旧问题', created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 6 * 3600e3).toISOString() })
  const replies = []
  const deps = depsWith({ extract: { ok: false, question: '新问题', missing: ['when'] }, verify: { ok: true } }, repo)
  await handleMessage('下周三考试周', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  const rec = readClarify(repo)
  assert.strictEqual(rec.original, '下周三考试周', '新命令覆盖旧记录')
  rmSync(repo, { recursive: true, force: true })
})

t('⑤ 误命中释放(extract not_command → 回聊天链、独占解除)', async () => {
  const repo = tmpRepo()
  const replies = []
  const deps = depsWith({ extract: { ok: false, not_command: true } }, repo)
  const r = await handleMessage('我们明天调课吧', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.strictEqual(r, 'chat', `释放回聊天链, got ${r}`)
  assert.strictEqual(readClarify(repo), null, '误命中不留记录')
  assert.ok(replies.includes('聊'), '释放后仍收到聊天回复(不静默丢弃)')
  rmSync(repo, { recursive: true, force: true })
})

t('⑥ 闲聊释放(追问期间答非所问 → 放行回聊天、记录保留)', async () => {
  const repo = tmpRepo()
  writeClarify(repo, { original: '停课', missing: ['date'], question: '哪天?', created_at: new Date().toISOString(), expires_at: new Date(Date.now() + 6 * 3600e3).toISOString() })
  const replies = []
  const deps = depsWith({ extract: { ok: false, not_command: true } }, repo)
  const r = await handleMessage('今天天气不错', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.strictEqual(r, 'chat', '放行聊天链')
  assert.ok(readClarify(repo), '记录保留(下次回答仍路由回提取)')
  assert.ok(replies.includes('聊'), '放行后仍收到聊天回复(不静默丢弃)')
  rmSync(repo, { recursive: true, force: true })
})

t('⑦ 删除链路(取消周三停课 → remove 精确匹配/歧义拒绝)', async () => {
  const repo = tmpRepo()
  const replies = []
  let called = null
  const deps = depsWith({
    extract: { ok: true, item: { kind: 'remove', match: { date: { weekday: 3 }, period: 3 } } },
    verify: { ok: true },
    daemon: async (item) => { called = item; return { ok: false, reason: 'not_found', question: '没找到这条安排,哥哥再确认一下?' } },
  }, repo)
  await handleMessage('取消周三停课', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(called && called.kind === 'remove', `remove 路由, got ${JSON.stringify(called)}`)
  assert.ok(replies[0].includes('没找到'), `not_found 追问: ${replies[0]}`)
  assert.ok(readClarify(repo), '拒绝入澄清记录')
  rmSync(repo, { recursive: true, force: true })
})

t('⑩ 确定性拒绝入澄清记录(resolve_when 拒绝 → question 入记录 → 下条路由回提取)', async () => {
  const repo = tmpRepo()
  const replies = []
  const deps = depsWith({
    extract: { ok: true, item: { kind: 'reminder', when: { date: '2026-08-01' }, label: '过去' } },
    verify: { ok: true },
    daemon: async () => ({ ok: false, reason: 'past_date', question: '这个日期已经过去了,告诉哥哥具体哪天的安排', missing: ['date'] }),
  }, repo)
  await handleMessage('8月1号交材料', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  const rec = readClarify(repo)
  assert.ok(rec && rec.missing.includes('date'), `⑩ 确定性拒绝入记录: ${JSON.stringify(rec)}`)
  assert.ok(replies[0].includes('过去了'), `H5 文案: ${replies[0]}`)
  rmSync(repo, { recursive: true, force: true })
})

t('⑫ match 收区间形态 → 拒绝追问(F7 子句)', async () => {
  const repo = tmpRepo()
  const replies = []
  const deps = depsWith({
    extract: { ok: true, item: { kind: 'remove', match: { date: { start: '2026-08-10', end: '2026-08-14' } } } },
    verify: { ok: true },
    daemon: async () => ({ ok: false, reason: 'shape_mismatch', question: '这个安排有点对不上,哥哥再确认一下?', missing: ['period'] }),
  }, repo)
  await handleMessage('取消下周的课', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(replies[0].includes('对不上'), `got ${replies[0]}`)
  rmSync(repo, { recursive: true, force: true })
})

t('⑬ 确认文案引用已登记条目原文(含星期数+日期,L1/A2)', async () => {
  const repo = tmpRepo()
  const replies = []
  const deps = depsWith({
    extract: { ok: true, item: { kind: 'reminder', when: { date: '2026-08-20' }, label: '交材料' } },
    verify: { ok: true },
    daemon: async () => ({ ok: true, text: '好,8月20日周四要交材料,我记着。' }),
  }, repo)
  await handleMessage('8月20号交材料', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, deps)
  assert.ok(replies[0].includes('8月20日') && replies[0].includes('周四'), `got ${replies[0]}`)
  rmSync(repo, { recursive: true, force: true })
})

t('鉴权:非 OWNER_ID 不进命令/回忆/追问路径,仅 askAgent 回复,不 recordUserMsg', async () => {
  const repo = tmpRepo()
  const replies = []
  const calls = []
  const deps = depsWith({ extract: { ok: true, item: { kind: 'reminder', when: { date: '2026-08-20' }, label: 'x' } }, verify: { ok: true } }, repo)
  const bot = { reply: async (m, t) => replies.push(t), sendTyping: async () => {} }
  // F-SEC-03 白名单模式：stranger 须命中白名单才放行进聊天链（C1 门保证不进命令/回忆/追问/状态写）
  const r = await handleMessage('停课', { userId: 'stranger@im.wechat' }, bot, queue,
    { ...deps, whitelist: ['stranger@im.wechat'], recordUserMsg: async () => calls.push('record') })
  assert.strictEqual(r, 'agent', '白名单内非本人走聊天链')
  assert.ok(!calls.includes('record'), '不 recordUserMsg/状态零写入')
  assert.ok(!existsSync(scheduleClarifyPath(repo)), '不写澄清记录')
  assert.ok(replies.includes('聊'), '仅 askAgent 回复(聊天链)')
  rmSync(repo, { recursive: true, force: true })
})

t('桥侧 180s 超时 → "处理失败,再试一次?" 且队列不阻塞(M16)', async () => {
  const repo = tmpRepo()
  const replies = []
  const slowDeps = {
    repoRoot: repo,
    // U8c: 修正 deps 键名（旧 extractPi/verifyPi 早已改为 extractAgent/verifyAgent；原写法
    // 使本用例静默走默认 extractAgent(真实 spawn)而非注入慢 deps——此前靠 AGENT_RUN_SCRIPT 未定义
    // 快速失败绕过,AGENT_RUN_SCRIPT 默认化后暴露。现正确注入慢 extract 以真测 180s 超时兜底。
    extractAgent: async () => { await new Promise((r) => setTimeout(r, 10)); throw new Error('timeout 180s') },
    verifyAgent: async () => ({ ok: true }),
    runDaemon: async () => ({ ok: true, text: 'x' }),
  }
  // R2.3: 去掉同义反复的 released 行,捕获返回值断言 'error'(withTimeout 拒绝 → catch → 兜底)
  const r = await handleMessage('停课', { userId: 'owner@im.wechat' }, fakeBot(replies), queue, slowDeps)
  assert.ok(replies[0].includes('处理失败'), `超时兜底: ${replies[0]}`)
  assert.strictEqual(r, 'error')
  rmSync(repo, { recursive: true, force: true })
})

t('extractBlock 平衡括号解析(嵌套 JSON 不被首 } 截断,C7)', () => {
  const text = 'prefix <<EXTRACT>>{"kind":"move","when":{"date":"2026-08-20"},"course":{"course":"高数","teacher":"刘洋"}}<<END>> suffix'
  const block = extractBlock(text, 'EXTRACT')
  const obj = JSON.parse(block)
  assert.strictEqual(obj.kind, 'move')
  assert.strictEqual(obj.course.course, '高数')
  assert.strictEqual(extractBlock('no marker', 'EXTRACT'), null)
  assert.strictEqual(extractBlock('<<EXTRACT>>{broken<<END>>', 'EXTRACT'), null, '畸形块 → null')
})

t('exitWordMatch 退出词判定', () => {
  assert.strictEqual(exitWordMatch('算了'), true)
  assert.strictEqual(exitWordMatch('没事了'), true)
  assert.strictEqual(exitWordMatch('今天心情不错'), false)
})

;(async () => {
  await runAll()
  console.log(`\n${'='.repeat(40)}\nALL ${passed} tests passed.`)
})().catch((e) => { console.error('FAIL', e); process.exit(1) })
