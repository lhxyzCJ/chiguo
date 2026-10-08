// chiguo-context.mjs — 迟菓 Pi extension：v2 runtime 上下文注入 + transcript 回写（Issue #477 Phase 7 预备）
//
// 事件接线（对齐 Pi extensions.md 契约）：
//  1. before_agent_start：GET <runtime>/context?session=<Pi session id>，把返回的上下文块
//     增量挂到 event.systemPromptOptions.sections['chiguo-context']（Pi 会据此向转录追加段落增量；
//     绝不返回 systemPrompt 整段替换）；
//  2. message_end：user/assistant 消息定稿时 POST <runtime>/turn {session, role, text, at}。
//     注意：Pi 对用户输入消息同样会发 message_end（agent-loop 初始/排队消息），故无需另注册 input。
//  3. fail-open：runtime 不可达 / 超时 / 非 JSON / HTTP 非 2xx → 跳过本次注入或回写，
//     不阻塞 Pi；stderr 一行告警，进程内限频（60s）。
//
// 纯 ESM JavaScript：无 TS 类型 import、无 npm 依赖，node 可直接执行，Pi（jiti）亦可加载。
// 端点契约与加载方式见 integrations/pi/README.md。

export const DEFAULT_RUNTIME_URL = 'http://127.0.0.1:8790'
export const DEFAULT_TIMEOUT_MS = 500
export const WARN_INTERVAL_MS = 60_000

// runtime /context 字段 → system prompt 小节（顺序即输出顺序；claude/3.11 契约见 docs/architecture-v2.md §3.11）
const CONTEXT_SECTIONS = [
  ['personality', '人格指引'],
  ['relationship', '关系摘要'],
  ['agenda', '当前议程'],
  ['memories', '相关记忆'],
  ['intent', '当前意图'],
  ['world', '世界状态'],
]

/** 解析 runtime 配置：opts 优先于 env，坏值回退默认。 */
export function resolveRuntimeConfig(env = process.env, opts = {}) {
  const rawUrl = opts.baseUrl ?? env?.CHIGUO_RUNTIME_URL
  const baseUrl = typeof rawUrl === 'string' && rawUrl.trim()
    ? rawUrl.trim().replace(/\/+$/, '')
    : DEFAULT_RUNTIME_URL
  const rawTimeout = opts.timeoutMs ?? Number(env?.CHIGUO_RUNTIME_TIMEOUT_MS)
  const timeoutMs = Number.isFinite(rawTimeout) && rawTimeout > 0 ? rawTimeout : DEFAULT_TIMEOUT_MS
  return { baseUrl, timeoutMs }
}

/**
 * 单个上下文字段 → 非空行数组。
 * 容错：string（按行拆分）/ string[] / [{text|summary|label|title}]；其余类型 → []。
 */
function sectionLines(value) {
  if (typeof value === 'string') {
    return value.split('\n').map((line) => line.trim()).filter((line) => line.length > 0)
  }
  if (!Array.isArray(value)) return []
  const lines = []
  for (const item of value) {
    if (typeof item === 'string') {
      if (item.trim()) lines.push(item.trim())
      continue
    }
    if (item && typeof item === 'object') {
      const text = item.text ?? item.summary ?? item.label ?? item.title
      if (typeof text === 'string' && text.trim()) lines.push(text.trim())
    }
  }
  return lines
}

/**
 * runtime /context JSON → 可追加的 system prompt 文本块。
 * 字段缺失/为空/类型不符 → 跳过该段（不产生空标题）；全空 → 返回 ''（调用方不追加任何内容）。
 */
export function buildContextBlock(ctx) {
  if (!ctx || typeof ctx !== 'object' || Array.isArray(ctx)) return ''
  const parts = []
  for (const [key, title] of CONTEXT_SECTIONS) {
    const lines = sectionLines(ctx[key])
    if (lines.length === 0) continue
    parts.push(`### ${title}\n${lines.join('\n')}`)
  }
  if (parts.length === 0) return ''
  return `## 迟菓运行时上下文\n\n${parts.join('\n\n')}`
}

/** AbortController 超时包装：超时后 fetch 以 signal.reason 拒绝，保证调用方有界等待。 */
async function withTimeout(timeoutMs, fn) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(new Error(`timeout ${timeoutMs}ms`)), timeoutMs)
  try {
    return await fn(controller.signal)
  } finally {
    clearTimeout(timer)
  }
}

function fetchFnOf(fetchImpl) {
  const fn = fetchImpl ?? globalThis.fetch
  if (typeof fn !== 'function') throw new Error('fetch unavailable')
  return fn
}

/** GET <baseUrl>/context?session=<session> → 解析后的 JSON（失败抛错，由 handler fail-open 兜底）。 */
export async function fetchContext(baseUrl, session, fetchImpl, opts = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS } = opts
  const fetchFn = fetchFnOf(fetchImpl)
  const url = `${String(baseUrl).replace(/\/+$/, '')}/context?session=${encodeURIComponent(session ?? '')}`
  return withTimeout(timeoutMs, async (signal) => {
    const res = await fetchFn(url, { method: 'GET', headers: { accept: 'application/json' }, signal })
    if (!res || !res.ok) throw new Error(`GET /context HTTP ${res?.status ?? '?'}`)
    return await res.json()
  })
}

/** POST <baseUrl>/turn，body = {session, role, text, at}（失败抛错，由 handler fail-open 兜底）。 */
export async function postTurn(baseUrl, turn, fetchImpl, opts = {}) {
  const { timeoutMs = DEFAULT_TIMEOUT_MS } = opts
  const fetchFn = fetchFnOf(fetchImpl)
  const url = `${String(baseUrl).replace(/\/+$/, '')}/turn`
  return withTimeout(timeoutMs, async (signal) => {
    const res = await fetchFn(url, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(turn),
      signal,
    })
    if (!res || !res.ok) throw new Error(`POST /turn HTTP ${res?.status ?? '?'}`)
    return true
  })
}

/** Pi 消息 content → 纯文本（只取 text part；thinking/toolCall/image 忽略）。 */
export function extractMessageText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  const parts = []
  for (const part of content) {
    if (part && typeof part === 'object' && part.type === 'text' && typeof part.text === 'string') {
      parts.push(part.text)
    }
  }
  return parts.join('\n')
}

/** 从 ExtensionContext 取 Pi session id；拿不到 → 'unknown'（fail-open）。 */
export function resolveSessionId(ctx) {
  try {
    const id = ctx?.sessionManager?.getSessionId?.()
    if (typeof id === 'string' && id.trim()) return id
  } catch { /* 访问 sessionManager 失败不阻塞主链 */ }
  return 'unknown'
}

/** message_end 事件 → /turn 请求体；非 user/assistant、无文本 → null（不发请求）。 */
export function messageToTurn(message, ctx) {
  const role = message?.role
  if (role !== 'user' && role !== 'assistant') return null
  const text = extractMessageText(message?.content).trim()
  if (!text) return null
  const at = typeof message?.timestamp === 'number' ? message.timestamp : Date.now()
  return { session: resolveSessionId(ctx), role, text, at }
}

/**
 * 把上下文块增量挂到 before_agent_start 的 systemPromptOptions.sections（不替换整个 system prompt）。
 * 返回追加后的段落文本；无结构化段落可挂/块为空 → null（本次不注入）。
 */
export function appendContextSection(event, block) {
  if (!block) return null
  const options = event?.systemPromptOptions
  if (!options || typeof options !== 'object') return null
  if (!options.sections || typeof options.sections !== 'object') options.sections = {}
  const key = 'chiguo-context'
  const prev = typeof options.sections[key] === 'string' ? options.sections[key] : ''
  options.sections[key] = prev ? `${prev}\n\n${block}` : block
  return options.sections[key]
}

/** 进程内限频告警：连续失败只在间隔外各报一行，且告警自身不得影响主链。 */
function createWarnLimiter(warnFn, intervalMs = WARN_INTERVAL_MS) {
  let lastAt = 0
  return (msg) => {
    const now = Date.now()
    if (now - lastAt < intervalMs) return
    lastAt = now
    try { warnFn(msg) } catch { /* 忽略 */ }
  }
}

/**
 * 注册 extension 事件（Pi: pi.on(name, handler)）。
 * opts: { baseUrl, timeoutMs, fetchImpl, session, warn, warnIntervalMs }（测试/开发注入用）。
 * 返回卸载函数（注销两个事件）。
 */
export function registerContextInjection(pi, opts = {}) {
  const { baseUrl, timeoutMs } = resolveRuntimeConfig(process.env, opts)
  const warn = createWarnLimiter(opts.warn ?? ((msg) => console.error(msg)), opts.warnIntervalMs)

  const offBefore = pi.on('before_agent_start', async (event, ctx) => {
    try {
      const session = opts.session ?? resolveSessionId(ctx)
      const context = await fetchContext(baseUrl, session, opts.fetchImpl, { timeoutMs })
      const block = buildContextBlock(context)
      if (block) appendContextSection(event, block)
    } catch (err) {
      warn(`[chiguo-context] GET /context 失败（fail-open，跳过注入）: ${err?.message ?? err}`)
    }
  })

  const offMessage = pi.on('message_end', async (event, ctx) => {
    try {
      const turn = messageToTurn(event?.message, ctx)
      if (turn) await postTurn(baseUrl, turn, opts.fetchImpl, { timeoutMs })
    } catch (err) {
      warn(`[chiguo-context] POST /turn 失败（fail-open，跳过回写）: ${err?.message ?? err}`)
    }
  })

  return () => {
    offBefore?.()
    offMessage?.()
  }
}

/** Pi extension 入口：pi --extension integrations/pi/extension/chiguo-context.mjs */
export default function chiguoContextExtension(pi) {
  registerContextInjection(pi, {})
}
