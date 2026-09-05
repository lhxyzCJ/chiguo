// test_cli_dto.mjs — wechat-bridge/cli-dto.mjs argv DTO 校验/组装测试（独立 runner，#391）
// 用法: node tests/test_cli_dto.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { agentAnalysisArgs, agentRecallArgs, agentExtractArgs, agentVerifyArgs, daemonMemorySearchArgs, daemonRecallArgs, daemonUserMsgArgs, daemonAnalysisArgs, daemonScheduleChangeArgs, healthRecordArgs, assertSpecialPayload, specialCommandArgs } from '../wechat-bridge/cli-dto.mjs'

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}: ${e.message}`); throw e }
  }
}
function throwsInvalid(fn) {
  assert.throws(fn, (e) => e instanceof TypeError || e instanceof RangeError)
}

// ── agent argv ──
t('agentAnalysisArgs: 正常组装 --prompt 原文 --analysis-mode', () => {
  assert.deepStrictEqual(agentAnalysisArgs('哥哥好'), ['--prompt', '哥哥好', '--analysis-mode'])
})
t('agentAnalysisArgs: 非字符串/空串 → 抛错', () => {
  for (const v of ['', null, undefined, 42, {}]) throwsInvalid(() => agentAnalysisArgs(v))
})
t('agentRecallArgs: 正常组装（facts 数组重序列化归一化）', () => {
  const facts = JSON.stringify([{ type: 'anniversary', label: '生日' }])
  assert.deepStrictEqual(agentRecallArgs('哪天生日', facts),
    ['--prompt', '哪天生日', '--schedule-recall', '--facts', facts])
})
t('agentRecallArgs: facts 非 JSON/非数组 → 抛错', () => {
  throwsInvalid(() => agentRecallArgs('q', '{bad'))
  throwsInvalid(() => agentRecallArgs('q', JSON.stringify({ a: 1 })))
  throwsInvalid(() => agentRecallArgs('', '[]'))
})
t('agentExtractArgs: 正常组装 + week-num 取 attention.week_num', () => {
  assert.deepStrictEqual(agentExtractArgs('下周加课吗', { week_num: 3, ok: true }),
    ['--prompt', '下周加课吗', '--schedule-extract', '--attention', JSON.stringify({ week_num: 3, ok: true }), '--week-num', '3'])
})
t('agentExtractArgs: attention 非对象 → {}；week_num 非数值 → 1', () => {
  const r = agentExtractArgs('q', null)
  assert.strictEqual(r[4], '{}')
  assert.strictEqual(r[6], '1')
  const r2 = agentExtractArgs('q', { week_num: 'x' })
  assert.strictEqual(r2[6], '1')
})
t('agentVerifyArgs: 正常组装（item 对象序列化）', () => {
  const item = { kind: 'add', course: '数学' }
  assert.deepStrictEqual(agentVerifyArgs('加课', item),
    ['--prompt', '加课', '--schedule-verify', '--item', JSON.stringify(item)])
})
t('agentVerifyArgs: item 非对象/非法 JSON → 抛错', () => {
  throwsInvalid(() => agentVerifyArgs('q', 42))
  throwsInvalid(() => agentVerifyArgs('q', '{bad'))
})

// ── daemon argv ──
t('daemonMemorySearchArgs/daemonRecallArgs: 正常组装', () => {
  assert.deepStrictEqual(daemonMemorySearchArgs('火锅'), ['--memory-search', '火锅'])
  assert.deepStrictEqual(daemonRecallArgs('生日'), ['--schedule-recall', '生日'])
})
t('daemonMemorySearchArgs/daemonRecallArgs: 空查询 → 抛错', () => {
  throwsInvalid(() => daemonMemorySearchArgs(''))
  throwsInvalid(() => daemonRecallArgs(null))
  throwsInvalid(() => daemonRecallArgs({ q: 1 }))
})
t('daemonUserMsgArgs: 无 recvId / 有 recvId', () => {
  assert.deepStrictEqual(daemonUserMsgArgs('原文'), ['--user-msg', '原文'])
  assert.deepStrictEqual(daemonUserMsgArgs('原文', 'id-1'), ['--user-msg', '原文', '--recv-id', 'id-1'])
})
t('daemonUserMsgArgs: 空文本 → 抛错；空 recvId → 静默跳过（沿旧 if (recvId) 语义）', () => {
  throwsInvalid(() => daemonUserMsgArgs(''))
  assert.deepStrictEqual(daemonUserMsgArgs('x', ''), ['--user-msg', 'x'])
})
t('daemonAnalysisArgs: 对象/JSON 字符串 analysis 均归一化', () => {
  const a = { warmth: 0.5 }
  assert.deepStrictEqual(daemonAnalysisArgs('原文', a, 'id-1'),
    ['--user-msg', '原文', '--analysis', JSON.stringify(a), '--recv-id', 'id-1'])
  assert.deepStrictEqual(daemonAnalysisArgs('原文', JSON.stringify(a)),
    ['--user-msg', '原文', '--analysis', JSON.stringify(a)])
})
t('daemonAnalysisArgs: 非法 analysis → 抛错', () => {
  throwsInvalid(() => daemonAnalysisArgs('原文', '{bad'))
  throwsInvalid(() => daemonAnalysisArgs('原文', 42))
})
t('daemonScheduleChangeArgs: 白名单 kind 通过', () => {
  for (const kind of ['reminder', 'add', 'cancel', 'move', 'exam_week', 'remove']) {
    const item = { kind, when: { date: '2026-08-20' } }
    assert.deepStrictEqual(daemonScheduleChangeArgs(item), ['--schedule-change', JSON.stringify(item)])
  }
})
t('daemonScheduleChangeArgs: 未知 kind/缺 kind/非对象 → 抛错', () => {
  throwsInvalid(() => daemonScheduleChangeArgs({ kind: 'drop_table' }))
  throwsInvalid(() => daemonScheduleChangeArgs({ when: {} }))
  throwsInvalid(() => daemonScheduleChangeArgs('["x"]'))
})

// ── health argv ──
t('healthRecordArgs: 白名单 outcome + reason 截断 100', () => {
  assert.deepStrictEqual(healthRecordArgs('fail', 'boom'), ['record', '--outcome', 'fail', '--reason', 'boom'])
  assert.deepStrictEqual(healthRecordArgs('success'), ['record', '--outcome', 'success'])
  const long = 'r'.repeat(200)
  const r = healthRecordArgs('send_fail', long)
  assert.strictEqual(r[4], long.slice(0, 100))
})
t('healthRecordArgs: 未知 outcome → 抛错', () => {
  throwsInvalid(() => healthRecordArgs('explode'))
  throwsInvalid(() => healthRecordArgs(''))
})

// ── 特殊命令 payload DTO ──
t('assertSpecialPayload: 五类标准 payload 归一化', () => {
  assert.deepStrictEqual(assertSpecialPayload({ kind: 'anniversary_add', date: '05-11', name: '生日' }),
    { kind: 'anniversary_add', date: '05-11', name: '生日' })
  assert.deepStrictEqual(assertSpecialPayload({ kind: 'anniversary_list' }), { kind: 'anniversary_list' })
  assert.deepStrictEqual(assertSpecialPayload({ kind: 'reminder', date: '2026-12-25', label: '考试' }),
    { kind: 'reminder', date: '2026-12-25', label: '考试' })
  assert.deepStrictEqual(assertSpecialPayload({ kind: 'break_on' }), { kind: 'break_on' })
  assert.deepStrictEqual(assertSpecialPayload({ kind: 'break_off' }), { kind: 'break_off' })
  const item = { kind: 'remove', match: { date: '2026-08-20' } }
  assert.deepStrictEqual(assertSpecialPayload({ kind: 'schedule_change', item }), { kind: 'schedule_change', item })
})
t('specialCommandArgs: 与旧双轨 daemon 字段逐字一致', () => {
  assert.deepStrictEqual(specialCommandArgs({ kind: 'anniversary_add', date: '05-11', name: '迟菓生日' }),
    ['--anniversary', 'add anniversary 05-11 迟菓生日'])
  assert.deepStrictEqual(specialCommandArgs({ kind: 'anniversary_list' }), ['--anniversary', 'list'])
  assert.deepStrictEqual(specialCommandArgs({ kind: 'reminder', date: '2026-12-25', label: '考试' }),
    ['--schedule-change', JSON.stringify({ kind: 'reminder', when: { date: '2026-12-25' }, label: '考试' })])
  assert.deepStrictEqual(specialCommandArgs({ kind: 'break_on' }), ['--break', 'on'])
  assert.deepStrictEqual(specialCommandArgs({ kind: 'break_off' }), ['--break', 'off'])
})

;(async () => {
  await runAll()
  console.log(`test_cli_dto: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1) })
