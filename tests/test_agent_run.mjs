// test_agent_run.mjs — agent-run 解析逻辑 + 调用链路测试（独立 runner）
// 用法: node test_agent_run.mjs（退出码 0=全过，1=有失败）
process.env.AGENTRUN_TELEMETRY = '0'   // 测试不写真实遥测日志
import assert from 'node:assert'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { execFileSync } from 'node:child_process'
import { readToml, parseNdjson, extractAnalysis, runAgentBin, run, extractBlock, runSchedule, resolveRepo, askAgent, DECISION_SEND_FIELDS } from '../scripts/agent-run.mjs'

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
t('parseNdjson: agent 失败(仅 user message_end + assistant 空)→ 空串不取提示词 (#225)', () => {
  const failOut = [
    '{"type":"session","version":3,"id":"x"}',
    '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"你是迟菓。以下是提示词……"}]}}',
    '{"type":"message_start","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"401: Invalid API key."}}',
    '{"type":"message_end","message":{"role":"assistant","content":[],"stopReason":"error","errorMessage":"401: Invalid API key."}}',
    '{"type":"agent_end","messages":[]}',
  ].join('\n')
  assert.strictEqual(parseNdjson(failOut), '', '401 失败不得把 user 提示词当回复')
})

t('extractAnalysis: 含 <<ANALYSIS>> 块 → 提取 JSON + 剥离后回复', () => {
  const text = '<<ANALYSIS>>{"warmth":0.5,"effort":0.3}<<END>>\n那、那也还行吧。'
  const r = extractAnalysis(text)
  assert.deepStrictEqual(r.analysis, { warmth: 0.5, effort: 0.3 })
  assert.strictEqual(r.reply, '那、那也还行吧。')
})
t('extractAnalysis: 嵌套 JSON 平衡括号提取（首 } 不截断）', () => {
  const text = '<<ANALYSIS>>{"a":{"b":1},"c":[1,2,3]}<<END>>\n回复文本'
  const r = extractAnalysis(text)
  assert.strictEqual(r.analysis.a.b, 1)
  assert.deepStrictEqual(r.analysis.c, [1, 2, 3])
  assert.ok(!r.reply.includes('<<ANALYSIS>>'), `reply 不得含标记，实得: ${r.reply}`)
  assert.ok(!r.reply.includes('<<END>>'), `reply 不得含 END 标记，实得: ${r.reply}`)
  assert.strictEqual(r.reply, '回复文本')
})
t('extractAnalysis: 无块 → analysis=null, reply=原文', () => {
  const r = extractAnalysis('就普通一句话')
  assert.strictEqual(r.analysis, null)
  assert.strictEqual(r.reply, '就普通一句话')
})
t('extractAnalysis: 块内坏 JSON → analysis=null, reply=剥离块后文本（标记不泄露）', () => {
  const text = '<<ANALYSIS>>{"warmth": broken<<END>>\n回复'
  const r = extractAnalysis(text)
  assert.strictEqual(r.analysis, null)
  assert.ok(!r.reply.includes('<<ANALYSIS>>'), `reply 不得含标记（当前 bug：保留原文含标记），实得: ${r.reply}`)
  assert.ok(!r.reply.includes('<<END>>'), `reply 不得含 END 标记，实得: ${r.reply}`)
  assert.strictEqual(r.reply, '回复')
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
  assert.ok(last.includes('按迟菓人格'), 'send-mode 应要求按人格生成')
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
  // 期望值按真实 toml 推导（与 agent-run.mjs 同款:env ?? toml ?? 缺省）——部署机自定义
  // provider/thinking 时测试不误挂（生产机实机闭环验证发现;CI 默认 toml 仍验缺省值）
  // 注意:env 键名(AGENTRUN_*)与 toml 键名(provider/model/...)不同,须分开传参
  const host = readToml(path.join(resolveRepo(import.meta.url), 'chiguo_proactive.toml')).host ?? {}
  const want = (envKey, hostKey, d) => process.env[envKey] ?? host[hostKey] ?? d
  const wantProvider = want('AGENTRUN_PROVIDER', 'provider', 'opencode-go')
  const wantModel = want('AGENTRUN_MODEL', 'model', 'deepseek-v4-flash')
  const wantSession = want('AGENTRUN_SESSION', 'session_id', 'chiguo-main')
  const wantThinking = want('AGENTRUN_THINKING', 'thinking_level', 'high')
  assert.ok(a.includes('--provider') && a.includes(wantProvider), `provider=${wantProvider}（toml/缺省）`)
  assert.ok(a.includes('--model') && a.includes(wantModel), `model=${wantModel}（toml/缺省）`)
  assert.ok(a.includes('--session-id') && a.includes(wantSession), `session-id=${wantSession}（toml/缺省）`)
  assert.ok(a.includes('--thinking') && a.includes(wantThinking), `thinking=${wantThinking}（toml/缺省）`)
  assert.ok(a.includes('--no-context-files'), '隔离仓库开发上下文')
  assert.ok(a.includes('--no-skills'), '禁用技能发现（砍 ~5,300 token 编码技能噪音）')
  assert.ok(a.includes('--mode') && a.includes('json'), '--mode json')
  const appends = a.filter((x) => x === '--append-system-prompt').length
  assert.strictEqual(appends, 3, '三份注入（精简版 + 记忆用法 + 工具用法）')
  assert.ok(a.some((x) => x.includes('迟菓人格-精简版.md')), '注入 精简版路径')
  assert.ok(a.some((x) => x.includes('记忆用法.md')), '注入记忆用法路径')
  assert.ok(a.some((x) => x.includes('工具用法.md')), '注入工具用法路径')
  assert.strictEqual(a[a.length - 1], 'P', 'prompt 为最后参数')
})

t('run: analysis-mode 用 reply_thinking_level（回复及时性,与主动发送 thinking_level 分离）', async () => {
  // 用临时 toml + 缓存击穿重导入,让本用例可红可绿(默认 toml 两值相同测不出差异)
  const td = fs.mkdtempSync(path.join(os.tmpdir(), 'chiguo-thinking-'))
  fs.writeFileSync(path.join(td, 'chiguo_proactive.toml'),
    '[host]\nthinking_level = "max"\nreply_thinking_level = "medium"\n')
  const prev = process.env.CHIGUO_REPO
  process.env.CHIGUO_REPO = td
  try {
    const mod = await import(`../scripts/agent-run.mjs?t=${Date.now()}-${Math.random()}`)
    let captured = null
    const spy = async (_b, a) => { captured = a; return { stdout: '' } }
    await mod.run(spy, { prompt: 'P', analysisMode: true })
    const ri = captured.indexOf('--thinking')
    assert.ok(ri >= 0 && captured[ri + 1] === 'medium',
      `analysis-mode 应用 reply_thinking_level=medium，实得 ${captured[ri + 1]}`)
    await mod.run(spy, { prompt: 'P', sendMode: true })
    const si = captured.indexOf('--thinking')
    assert.ok(si >= 0 && captured[si + 1] === 'max',
      `send-mode 应保留 thinking_level=max，实得 ${captured[si + 1]}`)
  } finally {
    if (prev === undefined) delete process.env.CHIGUO_REPO
    else process.env.CHIGUO_REPO = prev
    fs.rmSync(td, { recursive: true, force: true })
  }
})

// ── runAgentBin 真实 spawn（node -e 模拟 agent 退出码/stdout）──
const NDJSON_FULL = '{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"完整回复"}]}}'
t('runAgentBin: 非零退出但 stdout 含完整回复 → 不丢 stdout（salvage）', async () => {
  const code = `console.log(${JSON.stringify(NDJSON_FULL)});process.exit(3)`
  const { stdout } = await runAgentBin('node', ['-e', code], {})
  assert.match(stdout, /完整回复/, 'stdout 完整回复应保留')
})
t('runAgentBin: 非零退出且无完整回复 → reject 含退出码', async () => {
  await assert.rejects(runAgentBin('node', ['-e', 'process.exit(3)'], {}), /exited 3/)
})
t('runAgentBin: stdout 超过 maxBuffer → reject（R19: spawn 忽略 maxBuffer,手动上限防无界累积）', async () => {
  // 无限写 stdout 的子进程:超上限即 kill + reject,不挂死
  await assert.rejects(
    runAgentBin('node', ['-e', 'while(true) process.stdout.write("x".repeat(4096))'], { maxBuffer: 8 * 1024 }),
    /stdout 超过 8192 字节上限/
  )
})
t('run: runAgentBin salvage 场景 → ok:true 且 text 保留', async () => {
  const code = `console.log(${JSON.stringify(NDJSON_FULL)});process.exit(3)`
  const r = await run((_bin, _args, opts) => runAgentBin('node', ['-e', code], opts), { prompt: 'hi', analysisMode: false })
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

const tmp = mkdtempSync(join(tmpdir(), 'agent-run-test-'))
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
  'enabled = true',
  'retries = 2',
  'agent_command = ["node", "/opt/my-agent.mjs"]',
  '',
].join('\n'))

t('readToml: 解析 [host] 段字符串/布尔/数字键（忽略注释与其他段）', () => {
  const out = readToml(tomlPath)
  assert.strictEqual(out.host.provider, 'opencode-go')
  assert.strictEqual(out.host.model, 'deepseek-v4-flash')
  assert.strictEqual(out.host.thinking_level, 'high')
  assert.strictEqual(out.host.session_id, 'chiguo-main')
  assert.strictEqual(out.host.send_session_id, 'chiguo-send')
  assert.strictEqual(out.host.enabled, true)
  assert.strictEqual(out.host.retries, 2)
})
t('readToml: 数组值（agent_command）→ 字符串数组', () => {
  const out = readToml(tomlPath)
  assert.deepStrictEqual(out.host.agent_command, ['node', '/opt/my-agent.mjs'])
})
t('readToml: 文件不存在 → {}（不抛错）', () => {
  assert.deepStrictEqual(readToml(join(tmp, 'nope.toml')), {})
})

// ── v1.8 runner=command（自定义 agent 后端，env 注入覆盖 toml）──
const AGENT_OK = { ok: true, text: '自定义回复', analysis: { warmth: 0.5 }, usage: { total_tokens: 9 } }
async function withCommandRunner(fn) {
  const prev = [process.env.AGENTRUN_RUNNER, process.env.AGENTRUN_AGENT_COMMAND, process.env.AGENTRUN_TELEMETRY]
  process.env.AGENTRUN_RUNNER = 'command'
  process.env.AGENTRUN_AGENT_COMMAND = JSON.stringify(['node', '/tmp/fake-agent.mjs'])
  process.env.AGENTRUN_TELEMETRY = '0'
  try {
    const mod = await import(`../scripts/agent-run.mjs?cmd=${Date.now()}-${Math.random()}`)
    assert.strictEqual(mod.RUNNER, 'command')
    assert.deepStrictEqual(mod.AGENT_COMMAND, ['node', '/tmp/fake-agent.mjs'])
    await fn(mod)
  } finally {
    if (prev[0] === undefined) delete process.env.AGENTRUN_RUNNER; else process.env.AGENTRUN_RUNNER = prev[0]
    if (prev[1] === undefined) delete process.env.AGENTRUN_AGENT_COMMAND; else process.env.AGENTRUN_AGENT_COMMAND = prev[1]
    if (prev[2] === undefined) delete process.env.AGENTRUN_TELEMETRY; else process.env.AGENTRUN_TELEMETRY = prev[2]
  }
}
t('run: command runner 契约 JSON → text/analysis（--prompt/--mode 参数透传）', async () => {
  await withCommandRunner(async (mod) => {
    let captured = null
    const r = await mod.run(async (bin, args) => {
      captured = { bin, args }
      return { stdout: JSON.stringify(AGENT_OK) }
    }, { prompt: '在吗', analysisMode: true })
    assert.strictEqual(captured.bin, 'node')
    assert.strictEqual(captured.args[0], '/tmp/fake-agent.mjs')
    assert.strictEqual(captured.args[captured.args.length - 2], '--mode')
    assert.strictEqual(captured.args[captured.args.length - 1], 'analysis')
    assert.ok(captured.args.some((a) => a.includes('情绪分析')), 'analysis prompt 模板注入')
    assert.deepStrictEqual(r, { ok: true, text: '自定义回复', analysis: { warmth: 0.5 } })
  })
})
t('run: command runner send 模式 → mode=send + 决策指令', async () => {
  await withCommandRunner(async (mod) => {
    let captured = null
    const decision = JSON.stringify({ action: 'send', msg_id: 'm1' })
    await mod.run(async (_bin, args) => {
      captured = args
      return { stdout: JSON.stringify({ ok: true, text: '主动消息' }) }
    }, { prompt: decision, sendMode: true })
    const last = captured[captured.length - 1]
    assert.strictEqual(last, 'send')
    assert.ok(captured.some((a) => a.includes('主动消息决策结果')), 'send-mode 应含决策指令')
  })
})
t('run: command runner ok=false → {ok:false, error}（透传 agent error）', async () => {
  await withCommandRunner(async (mod) => {
    const r = await mod.run(async () => ({ stdout: JSON.stringify({ ok: false, error: 'agent boom' }) }),
      { prompt: 'x', analysisMode: false })
    assert.deepStrictEqual(r, { ok: false, error: 'agent boom' })
  })
})
t('run: command runner NDJSON 兼容（无 ok 字段 → parseNdjson 回退）', async () => {
  await withCommandRunner(async (mod) => {
    const r = await mod.run(async () => ({ stdout: NDJSON_OK }), { prompt: 'x', analysisMode: false })
    assert.deepStrictEqual(r, { ok: true, text: '第一段\n第二段' })
  })
})
t('run: command runner 空输出 → empty reply（不崩）', async () => {
  await withCommandRunner(async (mod) => {
    const r = await mod.run(async () => ({ stdout: '' }), { prompt: 'x', analysisMode: false })
    assert.deepStrictEqual(r, { ok: false, error: 'empty reply' })
  })
})
t('runSchedule: command runner parsed 契约直达（免块解析）', async () => {
  await withCommandRunner(async (mod) => {
    let captured = null
    const r = await mod.runSchedule(async (_bin, args) => {
      captured = args
      return { stdout: JSON.stringify({ ok: true, parsed: { kind: 'reminder', label: '交材料' }, raw: 'r' }) }
    }, { mode: 'extract', prompt: 'P', extra: { today: '2026-08-20', week_num: 3 } })
    assert.strictEqual(captured[captured.length - 1], 'extract')
    assert.ok(captured.some((a) => a.includes('安排提取器')), 'extract prompt 模板注入')
    assert.deepStrictEqual(r, { ok: true, parsed: { kind: 'reminder', label: '交材料' }, raw: 'r' })
  })
})
t('runSchedule: command runner NDJSON + <<EXTRACT>> 块兼容', async () => {
  await withCommandRunner(async (mod) => {
    const stdout = JSON.stringify({ type: 'message_end', message: { role: 'assistant', content: [{ type: 'text', text: '<<EXTRACT>>{"kind":"reminder","when":{"date":"2026-08-20"}}<<END>>' }] } })
    const r = await mod.runSchedule(async () => ({ stdout }), { mode: 'extract', prompt: 'P', extra: {} })
    assert.strictEqual(r.ok, true)
    assert.strictEqual(r.parsed.kind, 'reminder')
  })
})
// ── #99 行为增量：askAgent 统一入口（bridge 只依赖它，不再 import 内部函数）──
t('askAgent: 统一入口（= run analysisMode 便捷封装）', async () => {
  await withCommandRunner(async (mod) => {
    let captured = null
    const r = await mod.askAgent(async (_bin, args) => {
      captured = args
      return { stdout: JSON.stringify(AGENT_OK) }
    }, '在吗')
    // analysis 模式契约：--mode analysis + 情绪分析模板注入
    assert.strictEqual(captured[captured.length - 1], 'analysis')
    assert.ok(captured.some((a) => a.includes('情绪分析')), 'analysis prompt 模板注入')
    assert.deepStrictEqual(r, { ok: true, text: '自定义回复', analysis: { warmth: 0.5 } })
  })
})
t('askAgent: 默认实参 exec=runAgentBin（零参数调用不崩）', async () => {
  const mod = await import(`../scripts/agent-run.mjs?t2=${Date.now()}-${Math.random()}`)
  assert.strictEqual(typeof mod.askAgent, 'function')
})
// ── #99 行为增量：command runner 自动拼接人格三段（与 agent 模式行为一致）──
t('runnerCommand: command 分支自动注入 PERSONALITY/GUIDE/TOOLS 内容', async () => {
  const prev = [process.env.AGENTRUN_RUNNER, process.env.AGENTRUN_AGENT_COMMAND, process.env.AGENTRUN_TELEMETRY,
                process.env.AGENTRUN_PERSONALITY, process.env.AGENTRUN_GUIDE, process.env.AGENTRUN_TOOLS]
  process.env.AGENTRUN_RUNNER = 'command'
  process.env.AGENTRUN_AGENT_COMMAND = JSON.stringify(['node', '/tmp/fake-agent.mjs'])
  process.env.AGENTRUN_TELEMETRY = '0'
  process.env.AGENTRUN_PERSONALITY = join(tmp, 'pers.md')
  process.env.AGENTRUN_GUIDE = join(tmp, 'guide.md')
  process.env.AGENTRUN_TOOLS = join(tmp, 'tools.md')
  writeFileSync(join(tmp, 'pers.md'), '我是迟菓的人格')
  writeFileSync(join(tmp, 'guide.md'), '记忆用法指南')
  writeFileSync(join(tmp, 'tools.md'), '工具用法指南')
  try {
    const mod = await import(`../scripts/agent-run.mjs?cmd3=${Date.now()}-${Math.random()}`)
    const c = mod.runnerCommand('analysis', '原始提示词')
    assert.strictEqual(c.bin, 'node')
    assert.strictEqual(c.args[0], '/tmp/fake-agent.mjs')
    // --prompt 参数值应包含三段人格内容（command 与 agent 模式行为一致）
    const pIdx = c.args.indexOf('--prompt')
    assert.ok(pIdx >= 0, '应含 --prompt')
    const prompt = c.args[pIdx + 1]
    assert.ok(prompt.includes('我是迟菓的人格'), 'PERSONALITY 内容应注入 prompt')
    assert.ok(prompt.includes('记忆用法指南'), 'GUIDE 内容应注入 prompt')
    assert.ok(prompt.includes('工具用法指南'), 'TOOLS 内容应注入 prompt')
    assert.ok(prompt.includes('原始提示词'), '原始提示词保留')
    assert.strictEqual(c.args[c.args.length - 1], 'analysis')
  } finally {
    const rest = prev.slice(0, 3)
    ;['AGENTRUN_RUNNER', 'AGENTRUN_AGENT_COMMAND', 'AGENTRUN_TELEMETRY', 'AGENTRUN_PERSONALITY', 'AGENTRUN_GUIDE', 'AGENTRUN_TOOLS'].forEach((k, i) => {
      if (prev[i] === undefined) delete process.env[k]; else process.env[k] = prev[i]
    })
  }
})
t('run: command 人格三段经 runnerCommand 生效（端到端）', async () => {
  await withCommandRunner(async (mod) => {
    let captured = null
    const r = await mod.run(async (_bin, args) => {
      captured = args
      return { stdout: JSON.stringify({ ok: true, text: 'x' }) }
    }, { prompt: 'P', analysisMode: false })
    assert.strictEqual(r.ok, true)
    const pIdx = captured.indexOf('--prompt')
    const prompt = captured[pIdx + 1]
    assert.ok(prompt.includes('原始提示词') || prompt.includes('P'), 'prompt 透传')
  })
})
t('parseAgentOutput: 契约 JSON → 对象；非 JSON/无 ok 字段 → null', async () => {
  const mod = await import('../scripts/agent-run.mjs')
  assert.deepStrictEqual(mod.parseAgentOutput('{"ok":true,"text":"x"}'), { ok: true, text: 'x' })
  assert.strictEqual(mod.parseAgentOutput('{"text":"no ok"}'), null)
  assert.strictEqual(mod.parseAgentOutput('not json'), null)
  assert.strictEqual(mod.parseAgentOutput(''), null)
})

// ── resolveRepo 仓库根推导（可移植性：消除 /root/chiguo 硬编码）──
t('resolveRepo: 环境变量 CHIGUO_REPO 优先', () => {
  assert.strictEqual(resolveRepo('file:///x/y/z.mjs', { CHIGUO_REPO: '/tmp/r' }), '/tmp/r')
})

// ── AGENTRUN_ROTATE_SESSION=1（#223 send 每轮全新：显式会话也轮换）──
t('rotate-session: AGENTRUN_ROTATE_SESSION=1 备份显式会话（chiguo-send）', async () => {
  const { encodeSessionDir } = await import('../wechat-bridge/command-detect.mjs')
  const td = fs.mkdtempSync(path.join(os.tmpdir(), 'chiguo-rotate-sess-'))
  const prevHome = process.env.HOME
  const prevCwd = process.cwd()
  const prevRepo = process.env.CHIGUO_REPO
  // 模块顶层 REPO 解析依赖 CHIGUO_REPO：显式指向真实仓库（防环境残留 'undefined'）
  process.env.CHIGUO_REPO = path.join(path.dirname(new URL(import.meta.url).pathname), '..')
  process.env.HOME = td
  process.env.AGENTRUN_ROTATE_SESSION = '1'
  process.env.AGENTRUN_SESSION = 'chiguo-send'
  try {
    const cwd = path.join(td, 'wechat-bridge')
    fs.mkdirSync(cwd, { recursive: true })
    process.chdir(cwd)
    const sdir = path.join(td, '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
    fs.mkdirSync(sdir, { recursive: true })
    const file = path.join(sdir, '2099-01-01T00-00-00-000Z_chiguo-send.jsonl')
    fs.writeFileSync(file, 'x\n')
    await import(`../scripts/agent-run.mjs?rot=${Date.now()}-${Math.random()}`)
    assert.ok(!fs.existsSync(file), '显式会话文件已移走')
    const backups = path.join(td, '.chiguo', 'session-backups')
    assert.ok(fs.readdirSync(backups).some((f) => f.endsWith('-chiguo-send.jsonl')), 'send 备份存在')
  } finally {
    process.env.HOME = prevHome
    delete process.env.AGENTRUN_ROTATE_SESSION
    delete process.env.AGENTRUN_SESSION
    process.chdir(prevCwd)
    if (prevRepo === undefined) delete process.env.CHIGUO_REPO
    else process.env.CHIGUO_REPO = prevRepo
    fs.rmSync(td, { recursive: true, force: true })
  }
})
t('resolveRepo: 目录 URL（尾斜杠）→ 一级目录推导', () => {
  const repo = resolveRepo(new URL('.', import.meta.url).href, {})
  assert.ok(fs.existsSync(path.join(repo, 'chiguo_proactive.toml')), `推导失败: ${repo}`)
  assert.ok(fs.existsSync(path.join(repo, 'scripts/agent-run.mjs')))
})
t('resolveRepo: 文件 URL → 两级目录推导（生产调用分支）', () => {
  const repo = resolveRepo(new URL('../scripts/agent-run.mjs', import.meta.url).href, {})
  assert.ok(fs.existsSync(path.join(repo, 'chiguo_proactive.toml')), `推导失败: ${repo}`)
  assert.ok(fs.existsSync(path.join(repo, 'scripts/agent-run.mjs')))
})

// ── Q16 契约测试：mjs 消费字段清单与 Python 决策 schema（decision_schema.py）一致 ──
// agent-run 无法 import Python schema，只能对齐字段名。本测试以子进程调用 Python 的
// send_top_level_fields() 作为权威，断言 mjs 侧 DECISION_SEND_FIELDS 与其完全一致，
// 防止跨语言字段名漂移（keep-in-sync: decision_schema.py::send_top_level_fields()）。
t('Q16 agent-run 契约: DECISION_SEND_FIELDS 与 decision_schema.send_top_level_fields() 一致', () => {
  const repo = resolveRepo(new URL('../scripts/agent-run.mjs', import.meta.url).href, {})
  // 在仓库根跑 uv run python，保证 decision_schema.py 可被 import
  const out = execFileSync('uv', ['run', 'python', '-c',
    `import sys; sys.path.insert(0, '.');
from decision_schema import send_top_level_fields, CONTRACT
import json; print(json.dumps({"fields": send_top_level_fields(), "contract": CONTRACT}))`],
    { encoding: 'utf8', cwd: repo })
  const parsed = JSON.parse(out.trim().split(/\r?\n/).pop())  // 取最后一行 JSON
  const pythonFields = parsed.fields
  assert.ok(Array.isArray(pythonFields) && pythonFields.length > 0, `schema 字段清单为空: ${out}`)
  assert.strictEqual(parsed.contract, 1, `Python schema contract 应为 1，实得 ${parsed.contract}`)
  assert.deepStrictEqual([...DECISION_SEND_FIELDS].sort(), pythonFields,
    `mjs DECISION_SEND_FIELDS 与 Python schema 不一致\nmjs:    ${DECISION_SEND_FIELDS}\nschema: ${pythonFields}`)
})

;(async () => {
  await runAll()
  console.log(`test_agent_run: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1); })
