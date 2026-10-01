// test_pi_extension.mjs — 迟菓 Pi extension 客户端骨架测试（Issue #477 Phase 7 预备）
// 用法: node tests/test_pi_extension.mjs（退出码 0=全过，非 0=有失败）
// 形态与 tests/test_agent_run.mjs 一致：无框架，assert + 退出码；假 runtime 用随机端口，跑完 close。
import assert from 'node:assert'
import http from 'node:http'
import chiguoExtension, {
  registerContextInjection,
  buildContextBlock,
  fetchContext,
  appendContextSection,
  messageToTurn,
  resolveRuntimeConfig,
  DEFAULT_RUNTIME_URL,
  DEFAULT_TIMEOUT_MS,
} from '../integrations/pi/extension/chiguo-context.mjs'

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

const noop = () => {}

// 假 pi：收集 handler（对齐 Pi ExtensionAPI 的 pi.on(name, handler) 契约）
function fakePi() {
  const handlers = new Map()
  return {
    handlers,
    on(name, handler) { handlers.set(name, handler); return () => handlers.delete(name) },
  }
}
function fakeCtx(sessionId = 'sess-test') {
  return { sessionManager: { getSessionId: () => sessionId } }
}

// 假 runtime：127.0.0.1 随机端口；记录原始请求；close 由调用方负责（可重复运行）
function startFakeRuntime({ context = {}, contextBody = null, contextStatus = 200 } = {}) {
  return new Promise((resolve) => {
    const requests = []
    const server = http.createServer((req, res) => {
      let body = ''
      req.on('data', (chunk) => { body += chunk })
      req.on('end', () => {
        requests.push({ method: req.method, url: req.url, headers: req.headers, body })
        if (req.method === 'GET' && req.url.startsWith('/context')) {
          const payload = contextBody ?? JSON.stringify(context)
          res.writeHead(contextStatus, { 'content-type': 'application/json' })
          res.end(payload)
          return
        }
        if (req.method === 'POST' && req.url === '/turn') {
          res.writeHead(200, { 'content-type': 'application/json' })
          res.end('{"ok":true}')
          return
        }
        res.writeHead(404, { 'content-type': 'application/json' })
        res.end('{}')
      })
    })
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port
      resolve({ server, port, requests, baseUrl: `http://127.0.0.1:${port}` })
    })
  })
}
function closeServer(server) {
  return new Promise((resolve) => server.close(resolve))
}
// 取一个已知未监听的端口：先 listen 再 close（连接必然 ECONNREFUSED）
async function deadPort() {
  const tmp = await startFakeRuntime({})
  await closeServer(tmp.server)
  return tmp.port
}

// ── a) 事件注册 ──────────────────────────────────────────────────────
t('a) registerContextInjection：注册 before_agent_start + message_end，返回卸载函数', () => {
  const pi = fakePi()
  const off = registerContextInjection(pi, { baseUrl: 'http://127.0.0.1:1', warn: noop })
  assert.strictEqual(typeof pi.handlers.get('before_agent_start'), 'function', '缺 before_agent_start')
  assert.strictEqual(typeof pi.handlers.get('message_end'), 'function', '缺 message_end')
  assert.strictEqual(typeof off, 'function', '应返回卸载函数')
  off()
  assert.ok(!pi.handlers.has('before_agent_start') && !pi.handlers.has('message_end'), '卸载应注销两个事件')
})

t('a2) 默认导出工厂（Pi 加载入口）：同样注册两个事件', () => {
  const pi = fakePi()
  chiguoExtension(pi)
  assert.strictEqual(typeof pi.handlers.get('before_agent_start'), 'function')
  assert.strictEqual(typeof pi.handlers.get('message_end'), 'function')
})

t('a3) resolveRuntimeConfig：env 默认值/覆盖/坏值回退', () => {
  assert.deepStrictEqual(resolveRuntimeConfig({}, {}), { baseUrl: DEFAULT_RUNTIME_URL, timeoutMs: DEFAULT_TIMEOUT_MS })
  assert.deepStrictEqual(
    resolveRuntimeConfig({ CHIGUO_RUNTIME_URL: 'http://127.0.0.1:9999/', CHIGUO_RUNTIME_TIMEOUT_MS: '750' }, {}),
    { baseUrl: 'http://127.0.0.1:9999', timeoutMs: 750 })
  // 坏值 → 默认；opts 优先于 env
  assert.deepStrictEqual(resolveRuntimeConfig({ CHIGUO_RUNTIME_TIMEOUT_MS: 'abc', CHIGUO_RUNTIME_URL: '   ' }, {}),
    { baseUrl: DEFAULT_RUNTIME_URL, timeoutMs: DEFAULT_TIMEOUT_MS })
  assert.deepStrictEqual(resolveRuntimeConfig({}, { baseUrl: 'http://127.0.0.1:1234', timeoutMs: 50 }),
    { baseUrl: 'http://127.0.0.1:1234', timeoutMs: 50 })
})

// ── b) before_agent_start：注入上下文且保留原 prompt ─────────────────
t('b) before_agent_start：GET /context 带 session；上下文增量挂到 sections，原 prompt 不动', async () => {
  const runtime = await startFakeRuntime({
    context: {
      personality: '你是迟菓。',
      relationship: '哥哥最近加班',
      agenda: '',
      memories: ['爱吃火锅', { text: '怕冷' }, null, '   '],
      intent: null,
      world: { unused: true },
    },
  })
  try {
    const pi = fakePi()
    registerContextInjection(pi, { baseUrl: runtime.baseUrl, timeoutMs: 1000, warn: noop })
    const event = {
      type: 'before_agent_start',
      prompt: '哥哥在吗',
      systemPrompt: '原始系统提示',
      systemPromptOptions: { sections: { preamble: '原始系统提示' }, appendSystemPrompt: '', promptGuidelines: [] },
    }
    const result = await pi.handlers.get('before_agent_start')(event, fakeCtx('chiguo-main'))
    assert.strictEqual(result, undefined, '不得返回 systemPrompt 整段替换')

    const block = event.systemPromptOptions.sections['chiguo-context']
    assert.ok(typeof block === 'string' && block.length > 0, 'chiguo-context 段应存在')
    assert.ok(block.includes('人格指引') && block.includes('你是迟菓。'))
    assert.ok(block.includes('关系摘要') && block.includes('哥哥最近加班'))
    assert.ok(block.includes('相关记忆') && block.includes('爱吃火锅') && block.includes('怕冷'))
    assert.ok(!block.includes('当前议程'), '空 agenda 不得产生空段')
    assert.ok(!block.includes('当前意图'), 'null intent 不得产生空段')
    assert.ok(!block.includes('世界状态'), '坏类型 world 不得产生段')

    assert.strictEqual(event.systemPromptOptions.sections.preamble, '原始系统提示', '原段落必须保留')
    assert.strictEqual(event.systemPromptOptions.appendSystemPrompt, '', 'appendSystemPrompt 不得被顶替')

    const get = runtime.requests.find((r) => r.method === 'GET')
    assert.strictEqual(get.url, '/context?session=chiguo-main', 'GET /context 应带 session query')
  } finally {
    await closeServer(runtime.server)
  }
})

t('b2) 已有 chiguo-context 段（热重载）→ 追加而非覆盖', () => {
  const event = { systemPromptOptions: { sections: { 'chiguo-context': '旧块' } } }
  appendContextSection(event, '新块')
  assert.strictEqual(event.systemPromptOptions.sections['chiguo-context'], '旧块\n\n新块')
  // 无 systemPromptOptions → 不注入（绝不替换整段 prompt）
  assert.strictEqual(appendContextSection({ systemPrompt: '原' }, '块'), null)
})

// ── c) fail-open：runtime 不可达 / 超时 / 非 JSON ────────────────────
t('c) runtime 不可达：handler 不抛、prompt 不变、告警限频（两次失败只报一次）', async () => {
  const port = await deadPort()
  const warnings = []
  const pi = fakePi()
  registerContextInjection(pi, { baseUrl: `http://127.0.0.1:${port}`, timeoutMs: 300, warn: (m) => warnings.push(m) })
  const event = { type: 'before_agent_start', prompt: 'hi', systemPromptOptions: { sections: { preamble: '原 prompt' } } }
  const handler = pi.handlers.get('before_agent_start')
  await handler(event, fakeCtx())
  assert.strictEqual(event.systemPromptOptions.sections['chiguo-context'], undefined, '不注入')
  assert.strictEqual(event.systemPromptOptions.sections.preamble, '原 prompt', '原 prompt 不变')
  assert.strictEqual(warnings.length, 1, '应有一条告警')
  assert.ok(String(warnings[0]).includes('[chiguo-context]'), '告警前缀')
  await handler(event, fakeCtx())
  assert.strictEqual(warnings.length, 1, '进程内限频：第二次不再告警')
})

t('c2) 超时（fetch 悬挂）：AbortController 生效，handler 按时返回且 prompt 不变', async () => {
  const hangingFetch = (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(init.signal.reason ?? new Error('aborted')))
  })
  const pi = fakePi()
  registerContextInjection(pi, { baseUrl: 'http://127.0.0.1:1', timeoutMs: 50, fetchImpl: hangingFetch, warn: noop })
  const event = { type: 'before_agent_start', prompt: 'hi', systemPromptOptions: { sections: { preamble: '原 prompt' } } }
  const startedAt = Date.now()
  await pi.handlers.get('before_agent_start')(event, fakeCtx())
  assert.ok(Date.now() - startedAt < 1000, '超时后应迅速返回')
  assert.strictEqual(event.systemPromptOptions.sections['chiguo-context'], undefined)
})

t('c3) 非 JSON 响应 / HTTP 5xx → fetchContext 抛错（由 handler fail-open 兜底）', async () => {
  const badJson = await startFakeRuntime({ contextBody: 'not json at all' })
  const http500 = await startFakeRuntime({ context: {}, contextStatus: 500 })
  try {
    await assert.rejects(() => fetchContext(badJson.baseUrl, 's', undefined, { timeoutMs: 1000 }))
    await assert.rejects(() => fetchContext(http500.baseUrl, 's', undefined, { timeoutMs: 1000 }))

    const pi = fakePi()
    registerContextInjection(pi, { baseUrl: badJson.baseUrl, timeoutMs: 1000, warn: noop })
    const event = { systemPromptOptions: { sections: { preamble: '原' } } }
    await pi.handlers.get('before_agent_start')(event, fakeCtx())
    assert.strictEqual(event.systemPromptOptions.sections['chiguo-context'], undefined)
  } finally {
    await closeServer(badJson.server)
    await closeServer(http500.server)
  }
})

// ── d) message_end → POST /turn ──────────────────────────────────────
t('d) message_end：user/assistant 定稿各 POST /turn 一次，body 形状 {session, role, text, at}', async () => {
  const runtime = await startFakeRuntime({})
  try {
    const pi = fakePi()
    registerContextInjection(pi, { baseUrl: runtime.baseUrl, timeoutMs: 1000, warn: noop })
    const handler = pi.handlers.get('message_end')
    await handler({
      type: 'message_end',
      message: {
        role: 'assistant',
        content: [{ type: 'text', text: '嗯，在的。' }, { type: 'thinking', thinking: '内部思考' }],
        timestamp: 1720000000000,
      },
    }, fakeCtx('chiguo-main'))
    await handler({
      type: 'message_end',
      message: { role: 'user', content: '哥哥在吗', timestamp: 1720000001000 },
    }, fakeCtx('chiguo-main'))

    const posts = runtime.requests.filter((r) => r.method === 'POST' && r.url === '/turn')
    assert.strictEqual(posts.length, 2, `应收到 2 次 /turn，实得 ${posts.length}`)
    assert.match(posts[0].headers['content-type'], /application\/json/)
    assert.deepStrictEqual(posts.map((p) => JSON.parse(p.body)), [
      { session: 'chiguo-main', role: 'assistant', text: '嗯，在的。', at: 1720000000000 },
      { session: 'chiguo-main', role: 'user', text: '哥哥在吗', at: 1720000001000 },
    ])
  } finally {
    await closeServer(runtime.server)
  }
})

t('d2) message_end：toolResult / 无文本消息 / 非消息对象 → 不发请求、不抛', async () => {
  const runtime = await startFakeRuntime({})
  try {
    const pi = fakePi()
    registerContextInjection(pi, { baseUrl: runtime.baseUrl, timeoutMs: 1000, warn: noop })
    const handler = pi.handlers.get('message_end')
    await handler({ message: { role: 'toolResult', content: [{ type: 'text', text: '工具输出' }] } }, fakeCtx())
    await handler({ message: { role: 'assistant', content: [{ type: 'thinking', thinking: '只有思考' }] } }, fakeCtx())
    await handler({}, fakeCtx())
    await handler(undefined, undefined)
    assert.strictEqual(runtime.requests.length, 0, '不应发出任何请求')
  } finally {
    await closeServer(runtime.server)
  }
})

t('d3) messageToTurn：缺失 session/timestamp 时回退 unknown / 当前时间', () => {
  const turn = messageToTurn({ role: 'user', content: 'hi' }, undefined)
  assert.strictEqual(turn.session, 'unknown')
  assert.ok(typeof turn.at === 'number' && turn.at > 0)
  assert.strictEqual(messageToTurn({ role: 'user', content: '   ' }, undefined), null)
  assert.strictEqual(messageToTurn({ role: 'toolResult', content: 'x' }, undefined), null)
})

// ── e) buildContextBlock 纯函数 ──────────────────────────────────────
t('e) buildContextBlock：缺失/空/坏类型字段不产生空段；全空返回空串', () => {
  assert.strictEqual(buildContextBlock(null), '')
  assert.strictEqual(buildContextBlock('字符串'), '')
  assert.strictEqual(buildContextBlock([]), '')
  assert.strictEqual(buildContextBlock({}), '')
  assert.strictEqual(buildContextBlock({
    personality: '   ', relationship: '', agenda: [], memories: [null, { text: '' }, 42, '  '], intent: {}, world: [],
  }), '', '全空字段应得空串（调用方不追加）')

  const block = buildContextBlock({ relationship: '哥哥最近加班很累', memories: ['爱吃火锅', { text: '怕冷' }] })
  assert.ok(block.includes('### 关系摘要') && block.includes('哥哥最近加班很累'))
  assert.ok(block.includes('### 相关记忆') && block.includes('爱吃火锅') && block.includes('怕冷'))
  assert.ok(!block.includes('人格指引'), '缺失字段不得有标题')
  assert.ok(!block.includes('当前议程') && !block.includes('当前意图') && !block.includes('世界状态'))
  assert.ok(!/\n{3,}/.test(block), `不得出现空段/连续空行: ${JSON.stringify(block)}`)
  assert.ok(!block.endsWith('\n'), '结尾不得有多余空行')
  // 多行字符串：内部空行被过滤（不产生空行）
  const multiline = buildContextBlock({ intent: '第一行\n\n  第二行  ' })
  assert.ok(multiline.includes('### 当前意图\n第一行\n第二行'), `多行应逐行过滤空行并 trim: ${JSON.stringify(multiline)}`)
})

t('e2) buildContextBlock：字符串字段多行 + 数组对象别名（summary/label/title）容错', () => {
  const block = buildContextBlock({
    intent: '先问问今天累不累\n\n再决定要不要提课表',
    agenda: [{ summary: '考试周复习' }, { label: '' }, { title: '周末计划' }, { bad: 'x' }],
  })
  assert.ok(block.includes('### 当前意图') && block.includes('先问问今天累不累') && block.includes('再决定要不要提课表'))
  assert.ok(block.includes('### 当前议程') && block.includes('考试周复习') && block.includes('周末计划'))
  assert.ok(!block.includes('bad'), '无别名键的对象应跳过')
})

;(async () => {
  await runAll()
  console.log(`test_pi_extension: ${passed}/${tests.length} passed`)
  if (passed !== tests.length) process.exit(1)
})().catch((e) => { console.error('FAIL', e); process.exit(1) })
