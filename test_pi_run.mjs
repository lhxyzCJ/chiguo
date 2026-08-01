// test_pi_run.mjs — pi-run 解析逻辑 + 调用链路测试（独立 runner，仿 test_trigger_script.js）
// 用法: node test_pi_run.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { readToml, parseNdjson, extractAnalysis, runPiBin, run } from './scripts/pi-run.mjs'

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
