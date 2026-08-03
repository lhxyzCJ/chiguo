// test_pi_run.mjs — pi-run 解析逻辑 + 调用链路测试（独立 runner）
// 用法: node test_pi_run.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { readToml, parseNdjson, extractAnalysis, runPiBin, run, extractBlock, runSchedule } from '../scripts/pi-run.mjs'

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

// ── NDJSON 样例（pi --mode json 实际格式，最后一条 message_end 为助手回复）──
const NDJSON_OK = [
  '{"type":"session","version":3,"id":"x"}',
  '{"type":"turn_start"}',
  '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"测试"}]}}',
  '{"type":"message_start","message":{"role":"assistant","content":[]}}',
  '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"第一段"},{"type":"text","text":"第二段"}]}}',
  '{"type":"agent_end","messages":[]}',
].join('\n')

t('parseNdjson: 正常 NDJSON → 取最后 message_end 的 text（多段 join）', () => {
  assert.strictEqual(parseNdjson(NDJSON_OK), '第一段\n第二段')
})
t('parseNdjson: 空 stdout → 空串', () => {
  assert.strictEqual(parseNdjson(''), '')
})
t('parseNdjson: 全坏 JSON 行 → 空串（不抛错）', () => {
  assert.strictEqual(parseNdjson('{bad\nnot json\n12345\n'), '')
})
t('parseNdjson: 坏行 + 好行混合 → 只取好行', () => {
  const s = '{bad\n' + NDJSON_OK + '\ngarbage line'
  assert.strictEqual(parseNdjson(s), '第一段\n第二段')
})
t('parseNdjson: 无 message_end（只有 message_start）→ 空串', () => {
  assert.strictEqual(parseNdjson('{"type":"message_start","message":{"content":[]}}'), '')
})

t('extractAnalysis: 含 <<ANALYSIS>> 块 → 提取 JSON + 剥离后回复', () => {
  const text = '<<ANALYSIS>>{"warmth":0.5,"effort":0.3}<<END>>\n那、那也还行吧。'
  const r = extractAnalysis(text)
  assert.deepStrictEqual(r.analysis, { warmth: 0.5, effort: 0.3 })
  assert.strictEqual(r.reply, '那、那也还行吧。')
})
t('extractAnalysis: 无块 → analysis=null, reply=原文', () => {
  const r = extractAnalysis('就普通一句话')
  assert.strictEqual(r.analysis, null)
  assert.strictEqual(r.reply, '就普通一句话')
})
t('extractAnalysis: 块内坏 JSON → analysis=null, reply=原文', () => {
  const text = '<<ANALYSIS>>{"warmth": broken<<END>>\n回复'
  const r = extractAnalysis(text)
  assert.strictEqual(r.analysis, null)
  assert.strictEqual(r.reply, text)
})

// ── 调用链路（mock execFileP）──
const okExec = async () => ({ stdout: NDJSON_OK })
const emptyExec = async () => ({ stdout: '' })
const badExec = async () => ({ stdout: '{broken\nnot json\n' })
const failExec = async () => { throw new Error('exec fail: boom') }

t('run: 正常 NDJSON → {ok:true, text}', async () => {
  const r = await run(okExec, { prompt: 'hi', analysisMode: false })
  assert.deepStrictEqual(r, { ok: true, text: '第一段\n第二段' })
})
t('run: 空回复（无 message_end 文本）→ {ok:false, error:empty reply}', async () => {
  const r = await run(emptyExec, { prompt: 'hi', analysisMode: false })
  assert.deepStrictEqual(r, { ok: false, error: 'empty reply' })
})
t('run: 坏 JSON 输出 → {ok:false, error:empty reply}（不崩溃）', async () => {
  const r = await run(badExec, { prompt: 'hi', analysisMode: false })
  assert.deepStrictEqual(r, { ok: false, error: 'empty reply' })
})
t('run: exec 抛错 → {ok:false, error:err.message}', async () => {
  const r = await run(failExec, { prompt: 'hi', analysisMode: false })
  assert.deepStrictEqual(r, { ok: false, error: 'exec fail: boom' })
})
t('run: --analysis-mode → 剥离分析块 + analysis 字段', async () => {
  const text = '<<ANALYSIS>>{"warmth":-0.2,"effort":0.8}<<END>>\n哥哥……今天有点累。'
  const stdout = JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text }] } })
  const r = await run(async () => ({ stdout }), { prompt: '累了吗', analysisMode: true })
  assert.strictEqual(r.ok, true)
  assert.deepStrictEqual(r.analysis, { warmth: -0.2, effort: 0.8 })
  assert.strictEqual(r.text, '哥哥……今天有点累。')
})
t('run: --send-mode → 决策指令包装（发送侧生成消息）', async () => {
  let captured = null
  const spyExec = async (_bin, args) => { captured = args; return { stdout: '' } }
  const decision = JSON.stringify({ action: 'send', msg_id: 'm1', context: { layer: 'middle' } })
  await run(spyExec, { prompt: decision, sendMode: true })
  const last = captured[captured.length - 1]
  assert.ok(last.includes('主动消息决策结果'), 'send-mode 应含决策指令')
  assert.ok(last.includes('按 SUN2.md 人格'), 'send-mode 应要求按人格生成')
  assert.ok(last.includes('1-3 句'), 'send-mode 应要求 1-3 句')
  assert.ok(last.includes('action=send'), 'send-mode 应标注 action=send')
})
t('run: piArgs 构造（provider/model/session/thinking/人格注入/--mode json）', async () => {
  let captured = null
  const spyExec = async (_bin, args) => { captured = { bin: _bin, args }; return { stdout: '' } }
  await run(spyExec, { prompt: 'P', analysisMode: false })
  assert.strictEqual(captured.bin, 'pi')
  const a = captured.args
  assert.strictEqual(a[0], '-p')
  assert.ok(a.includes('--provider') && a.includes('opencode-go'), 'provider 默认 opencode-go')
  assert.ok(a.includes('--model') && a.includes('deepseek-v4-flash'), 'model 默认 deepseek-v4-flash')
  assert.ok(a.includes('--session-id') && a.includes('chiguo-main'), 'session-id 默认 chiguo-main')
  assert.ok(a.includes('--thinking') && a.includes('high'), 'thinking 默认 high')
  assert.ok(a.includes('--no-context-files'), '隔离仓库开发上下文')
  assert.ok(a.includes('--mode') && a.includes('json'), '--mode json')
  const appends = a.filter((x) => x === '--append-system-prompt').length
  assert.strictEqual(appends, 2, '两份人格注入（SUN2 + 语言技巧指南）')
  assert.ok(a.some((x) => x.includes('SUN2.md')), '注入 SUN2.md 路径')
  assert.ok(a.some((x) => x.includes('迟菓语言技巧指南.md')), '注入语言技巧指南路径')
  assert.strictEqual(a[a.length - 1], 'P', 'prompt 为最后参数')
})

// ── runPiBin 真实 spawn（node -e 模拟 pi 退出码/stdout）──
const NDJSON_FULL = '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"完整回复"}]}}'
t('runPiBin: 非零退出但 stdout 含完整回复 → 不丢 stdout（salvage）', async () => {
  const code = `console.log(${JSON.stringify(NDJSON_FULL)});process.exit(3)`
  const { stdout } = await runPiBin('node', ['-e', code], {})
  assert.match(stdout, /完整回复/, 'stdout 完整回复应保留')
})
t('runPiBin: 非零退出且无完整回复 → reject 含退出码', async () => {
  await assert.rejects(runPiBin('node', ['-e', 'process.exit(3)'], {}), /pi exited 3/)
})
t('run: runPiBin salvage 场景 → ok:true 且 text 保留', async () => {
  const code = `console.log(${JSON.stringify(NDJSON_FULL)});process.exit(3)`
  const r = await run((_bin, _args, opts) => runPiBin('node', ['-e', code], opts), { prompt: 'hi', analysisMode: false })
  assert.deepStrictEqual(r, { ok: true, text: '完整回复' })
})

// ── extractBlock 通用块提取器（批次 6a,C7 平衡括号解析）──
t('extractBlock: 嵌套 JSON 平衡括号提取（首 } 不截断）', () => {
  const text = 'prefix <<EXTRACT>>{"kind":"move","when":{"date":"2026-08-20"},"course":{"course":"高数","teacher":"刘洋"}}<<END>> suffix'
  const block = extractBlock(text, 'EXTRACT')
  const obj = JSON.parse(block)
  assert.strictEqual(obj.kind, 'move')
  assert.strictEqual(obj.course.course, '高数')
  assert.strictEqual(extractBlock('no marker', 'EXTRACT'), null, '无 marker → null')
  assert.strictEqual(extractBlock('<<EXTRACT>>{broken<<END>>', 'EXTRACT'), null, '括号不平衡 → null')
})
t('extractBlock: 字符串内 } 不参与配对（忽略字符串内容）', () => {
  const text = 'x <<EXTRACT>>{"label":"a}b","when":{"date":"2026-08-20"}}<<END>> y'
  const block = extractBlock(text, 'EXTRACT')
  assert.strictEqual(JSON.parse(block).label, 'a}b')
  assert.deepStrictEqual(JSON.parse(block).when, { date: '2026-08-20' })
})
t('extractBlock: 非 { 开头 → null；不同 marker 独立', () => {
  assert.strictEqual(extractBlock('<<EXTRACT>>hello<<END>>', 'EXTRACT'), null, '非 JSON 块 → null')
  assert.strictEqual(extractBlock('a <<VERIFY>>{"ok":false,"question":"q"}<<END>> b', 'VERIFY'),
    '{"ok":false,"question":"q"}')
  assert.strictEqual(extractBlock('a <<VERIFY>>{"ok":false,"question":"q"}<<END>> b', 'EXTRACT'), null, 'marker 不匹配')
})

// ── runSchedule 新模式（批次 6a,独立会话 chiguo-extract/verify）──
t('runSchedule: extract 模式 → 解析 <<EXTRACT>> 块返回 parsed', async () => {
  const stdout = JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: '<<EXTRACT>>{"kind":"reminder","when":{"date":"2026-08-20"},"label":"交材料"}<<END>>' }] } })
  const r = await runSchedule(async () => ({ stdout }), { mode: 'extract', prompt: '8月20号交材料', extra: { today: '2026-08-20', week_num: 3 } })
  assert.strictEqual(r.ok, true)
  assert.strictEqual(r.parsed.kind, 'reminder')
  assert.strictEqual(r.parsed.when.date, '2026-08-20')
})
t('runSchedule: verify 模式 → ok:false + question/missing 透传', async () => {
  const stdout = JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: '<<VERIFY>>{"ok":false,"question":"哪天?","missing":["date"]}<<END>>' }] } })
  const r = await runSchedule(async () => ({ stdout }), { mode: 'verify', prompt: 'x', extra: { item: '{}' } })
  assert.strictEqual(r.ok, true)
  assert.strictEqual(r.parsed.ok, false)
  assert.strictEqual(r.parsed.missing[0], 'date')
})
t('runSchedule: 空回复 → ok:false empty reply', async () => {
  const r = await runSchedule(async () => ({ stdout: '' }), { mode: 'extract', prompt: 'x', extra: {} })
  assert.deepStrictEqual(r, { ok: false, error: 'empty reply' })
})
t('runSchedule: 无块 → ok:false malformed block', async () => {
  const stdout = JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: '没有块' }] } })
  const r = await runSchedule(async () => ({ stdout }), { mode: 'extract', prompt: 'x', extra: {} })
  assert.deepStrictEqual(r, { ok: false, error: 'malformed block' })
})
t('runSchedule: 块内非 JSON → ok:false block not json', async () => {
  const stdout = JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: '<<EXTRACT>>{oops}<<END>>' }] } })
  const r = await runSchedule(async () => ({ stdout }), { mode: 'extract', prompt: 'x', extra: {} })
  assert.deepStrictEqual(r, { ok: false, error: 'block not json' })
})
t('runSchedule: exec 抛错 → ok:false error message', async () => {
  const r = await runSchedule(async () => { throw new Error('boom') }, { mode: 'extract', prompt: 'x', extra: {} })
  assert.deepStrictEqual(r, { ok: false, error: 'boom' })
})
t('runSchedule: extract 独立会话 chiguo-extract + prompt 含原文/今天/周次', async () => {
  let captured = null
  const spyExec = async (_bin, args) => { captured = args; return { stdout: '' } }
  await runSchedule(spyExec, { mode: 'extract', prompt: 'P', extra: { today: '2026-08-20', week_num: 3 } })
  assert.ok(captured.includes('--session-id') && captured.includes('chiguo-extract'), '独立会话')
  const last = captured[captured.length - 1]
  assert.ok(last.includes('安排提取器'), 'extract 指令模板')
  assert.ok(last.includes('今天是2026-08-20'), '注入今天')
  assert.ok(last.includes('第 3 周'), '注入学期周次')
  assert.ok(last.includes('消息：P'), '原文最后注入')
})
t('runSchedule: verify 独立会话 chiguo-verify + item 注入', async () => {
  let captured = null
  const spyExec = async (_bin, args) => { captured = args; return { stdout: '' } }
  await runSchedule(spyExec, { mode: 'verify', prompt: '原文', extra: { item: '{"kind":"x"}' } })
  assert.ok(captured.includes('--session-id') && captured.includes('chiguo-verify'), '独立会话')
  const last = captured[captured.length - 1]
  assert.ok(last.includes('安排校验员'), 'verify 指令模板')
  assert.ok(last.includes('item：{"kind":"x"}'), 'item 注入')
})

// ── readToml 极简解析（临时 toml 文件）──
import { writeFileSync, mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const tmp = mkdtempSync(join(tmpdir(), 'pi-run-test-'))
const tomlPath = join(tmp, 'test.toml')
writeFileSync(tomlPath, [
  '# 注释行',
  '[other]',
  'x = 1',
  '',
  '[host]',
  'provider = "opencode-go"   # 内联注释',
  'model = "deepseek-v4-flash"',
  'thinking_level = "high"',
  'session_id = "chiguo-main"',
  'send_session_id = "chiguo-send"',
  'personality_dir = "/tmp/pp"',
  'enabled = true',
  'retries = 2',
  '',
].join('\n'))

t('readToml: 解析 [host] 段字符串/布尔/数字键（忽略注释与其他段）', () => {
  const out = readToml(tomlPath)
  assert.strictEqual(out.host.provider, 'opencode-go')
  assert.strictEqual(out.host.model, 'deepseek-v4-flash')
  assert.strictEqual(out.host.thinking_level, 'high')
  assert.strictEqual(out.host.session_id, 'chiguo-main')
  assert.strictEqual(out.host.send_session_id, 'chiguo-send')
  assert.strictEqual(out.host.personality_dir, '/tmp/pp')
  assert.strictEqual(out.host.enabled, true)
  assert.strictEqual(out.host.retries, 2)
})
t('readToml: 文件不存在 → {}（不抛错）', () => {
  assert.deepStrictEqual(readToml(join(tmp, 'nope.toml')), {})
})

;(async () => {
  await runAll()
  console.log(`test_pi_run: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1); })
