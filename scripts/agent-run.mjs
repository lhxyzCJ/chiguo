#!/usr/bin/env node
/**
 * agent-run — chiguo 的 pi-agent 调用统一封装。
 * 用法: node agent-run.mjs --prompt <文本> [--analysis-mode]
 * 配置: 环境变量或 toml [host] 段（AGENTRUN_* 覆盖）
 * 输出: {ok:true, text, analysis?} 或 {ok:false, error}
 */
import { spawn } from 'node:child_process'
import { readFileSync, mkdirSync, appendFileSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL, fileURLToPath } from 'node:url'

/** 仓库根推导：CHIGUO_REPO 环境变量优先，否则从脚本位置推导（文件 URL 两级、目录 URL 一级目录）。 */
export function resolveRepo(fileURL, env = process.env) {
  if (env.CHIGUO_REPO) return env.CHIGUO_REPO
  const p = fileURLToPath(fileURL)
  const dir = p.endsWith(path.sep) ? p : path.dirname(p)
  return path.dirname(dir)
}

const REPO = resolveRepo(import.meta.url)
const HOST = readToml(`${REPO}/chiguo_proactive.toml`)?.host ?? {}
// v1.8 agent 后端抽象：runner=agent（默认，pi-agent 二进制）| command（任意 CLI agent，
// 经 [host].agent_command 指定，契约见 doc/AGENT_INTEGRATION.md「接入自定义 agent」）。
// AGENTRUN_RUNNER/AGENTRUN_AGENT_COMMAND 环境变量可覆盖（AGENT_COMMAND 为 JSON 数组字符串）。
export const RUNNER = process.env.AGENTRUN_RUNNER ?? HOST.runner ?? 'agent'
export const AGENT_COMMAND = (() => {
  if (process.env.AGENTRUN_AGENT_COMMAND) {
    try { return JSON.parse(process.env.AGENTRUN_AGENT_COMMAND) } catch { return [] }
  }
  return Array.isArray(HOST.agent_command) ? HOST.agent_command : []
})()
const AGENT_BIN = process.env.AGENT_BIN ?? 'pi'
const PROVIDER = process.env.AGENTRUN_PROVIDER ?? HOST.provider ?? 'opencode-go'  // provider 可配：pi --provider 名（内置或 models.json 自定义）
const MODEL = process.env.AGENTRUN_MODEL ?? HOST.model ?? 'deepseek-v4-flash'
const THINKING = process.env.AGENTRUN_THINKING ?? HOST.thinking_level ?? 'high'
// 回复侧独立档位(交互路径要快):env AGENTRUN_REPLY_THINKING ?? toml reply_thinking_level ?? 回退 THINKING。
// 主动发送(send-mode)与命令/重分析路径保持 thinking_level,互不拖累(埋埋实机:max 单次 63s+,回复体验差)
const REPLY_THINKING = process.env.AGENTRUN_REPLY_THINKING ?? HOST.reply_thinking_level ?? THINKING
// agent 调用超时(ms):默认 120s;replan 等长任务经 AGENTRUN_TIMEOUT 覆盖(replan.py replan_env 注入)
export const AGENT_TIMEOUT = Number(process.env.AGENTRUN_TIMEOUT ?? 120_000)
const SESSION_ID = process.env.AGENTRUN_SESSION ?? HOST.session_id ?? 'chiguo-main'

// AGENTRUN_NEW_SESSION=1:执行前把当前 chiguo-main 会话移入备份(与微信 /new 共享逻辑),
// 本次调用从全新会话开始。仅对默认回复会话生效(AGENTRUN_SESSION 显式指定时不移)。
if (process.env.AGENTRUN_NEW_SESSION === '1' && !process.env.AGENTRUN_SESSION) {
  try {
    const { backupSessionFile } = await import(pathToFileURL(join(REPO, 'wechat-bridge', 'command-detect.mjs')))
    const { homedir } = await import('node:os')
    const dst = backupSessionFile(process.cwd(), join(homedir(), '.chiguo', 'session-backups'))
    if (dst) console.error(`[new-session] 旧会话已备份: ${dst}`)
  } catch (err) {
    console.error('[new-session] 备份失败:', err instanceof Error ? err.message : String(err))
  }
}
const PERSONALITY_DIR = HOST.personality_dir ?? `${REPO}/personality`
const PERSONALITY = process.env.AGENTRUN_PERSONALITY ?? `${PERSONALITY_DIR}/迟菓人格-精简版.md`
const GUIDE = process.env.AGENTRUN_GUIDE ?? `${PERSONALITY_DIR}/记忆用法.md`
const TOOLS = process.env.AGENTRUN_TOOLS ?? `${PERSONALITY_DIR}/工具用法.md`

export function readToml(p) {
  const out = {}
  let section = null
  try {
    for (const line of readFileSync(p, 'utf8').split('\n')) {
      const t = line.trim()
      if (!t || t.startsWith('#')) continue
      const sec = t.match(/^\[([^\]]+)\]/)
      if (sec) { section = sec[1]; out[section] = out[section] ?? {}; continue }
      if (!section) continue
      const m = t.match(/^([A-Za-z0-9_.-]+)\s*=\s*(.+)$/)
      if (!m) continue
      let v = m[2].replace(/\s+#.*$/, '').trim()
      const q = v.match(/^"(.*)"$/)
      if (q) v = q[1]
      else if (v === 'true') v = true
      else if (v === 'false') v = false
      else if (/^-?\d+(\.\d+)?$/.test(v)) v = Number(v)
      else if (v.startsWith('[') && v.endsWith(']')) {
        // v1.8: 数组值（如 agent_command = ["node", "/path/x.mjs"]）→ 字符串数组
        v = v.slice(1, -1).split(',')
          .map((s) => s.trim().replace(/^"(.*)"$/, '$1').replace(/^'(.*)'$/, '$1'))
          .filter((s) => s !== '')
      }
      out[section][m[1]] = v
    }
  } catch {}
  return out
}

export function parseNdjson(stdout) {
  let finalText = ''
  for (const line of stdout.split('\n')) {
    if (!line.trim()) continue
    try {
      const ev = JSON.parse(line)
      if (ev.type === 'message_end') {
        const texts = (ev.message?.content ?? [])
          .filter((c) => c.type === 'text').map((c) => c.text)
        if (texts.length) finalText = texts.join('\n')
      }
    } catch {}
  }
  return finalText
}

/** 从 NDJSON 事件流取最后一次 message_end 的 usage(含 DeepSeek prompt_cache_hit/miss_tokens)。 */
export function parseUsage(stdout) {
  let usage = null
  for (const line of stdout.split('\n')) {
    if (!line.trim()) continue
    try {
      const ev = JSON.parse(line)
      if (ev.type === 'message_end' && ev.message?.usage && Object.keys(ev.message.usage).length > 0) {
        usage = ev.message.usage
      }
    } catch {}
  }
  return usage
}

/** 遥测:一行一轮,追加写 {REPO}/logs/agent-run.log(gitignore)。/status 与验收依赖此文件。
 *  AGENTRUN_TELEMETRY=0 时跳过(测试环境)。 */
export function appendTelemetry(entry, repo = REPO) {
  try {
    if (process.env.AGENTRUN_TELEMETRY === '0') return
    const dir = `${repo}/logs`
    mkdirSync(dir, { recursive: true })
    appendFileSync(`${dir}/agent-run.log`, `${JSON.stringify(entry)}\n`)
  } catch {}
}

export function extractAnalysis(text) {
  const m = text.match(/<<ANALYSIS>>\s*(\{[\s\S]*?\})\s*<<END>>/)
  if (!m) return { analysis: null, reply: text }
  try {
    return { analysis: JSON.parse(m[1]), reply: text.replace(/<<ANALYSIS>>[\s\S]*?<<END>>/, '').trim() }
  } catch {
    return { analysis: null, reply: text }
  }
}

/** 通用块提取器:<<MARKER>>...<<END>>;平衡括号解析(嵌套 JSON 不被首 } 截断,C7);畸形 → null */
export function extractBlock(text, marker) {
  const start = text.indexOf(`<<${marker}>>`)
  if (start < 0) return null
  const body = text.slice(start + marker.length + 4)
  const end = body.indexOf('<<END>>')
  const raw = end >= 0 ? body.slice(0, end) : body
  const t = raw.trim()
  if (!t.startsWith('{')) return null
  let depth = 0, inStr = false, esc = false
  for (let i = 0; i < t.length; i++) {
    const ch = t[i]
    if (inStr) {
      if (esc) esc = false
      else if (ch === '\\') esc = true
      else if (ch === '"') inStr = false
    } else if (ch === '"') inStr = true
    else if (ch === '{') depth++
    else if (ch === '}') { depth--; if (depth === 0) return t.slice(0, i + 1) }
  }
  return null  // 括号不平衡 → 畸形
}

/** 用 spawn 收集 stdout（execFile 在 agent 二进制下会挂起：其等 stdin EOF，execFile 管道不关）。
 *  R19: spawn 忽略 maxBuffer（execFile 专属）→ 手动施加上限，超限即 kill + reject（防无界累积内存）。 */
export function runAgentBin(bin, args, opts) {
  return new Promise((resolve, reject) => {
    const maxBuffer = opts.maxBuffer ?? 16 * 1024 * 1024
    const c = spawn(bin, args, { stdio: ['ignore', 'pipe', 'pipe'], ...opts })
    let stdout = ''
    let stderr = ''
    let killed = false
    const bail = (err) => {
      if (killed) return
      killed = true
      try { c.kill('SIGKILL') } catch {}
      reject(err)
    }
    c.stdout.on('data', (d) => {
      stdout += d
      if (Buffer.byteLength(stdout, 'utf8') > maxBuffer) {
        bail(new Error(`${bin} stdout 超过 ${maxBuffer} 字节上限（输出无界，已终止）`))
      }
    })
    c.stderr.on('data', (d) => {
      stderr += d
      // 防 stderr 无界：仅保留尾部（错误诊断信息一般较短，截断不影响退出码语义）
      if (Buffer.byteLength(stderr, 'utf8') > 1024 * 1024) stderr = stderr.slice(-256 * 1024)
    })
    c.on('error', (err) => { if (!killed) reject(err) })
    c.on('close', (code, signal) => {
      if (killed) return
      // 非零退出但 stdout 已含完整回复（如 teardown/session 保存失败）→ 不丢回复；
      // parseNdjson 取最后 message_end，无完整回复仍按失败处理
      if (code !== 0 && !parseNdjson(stdout) && !parseAgentOutput(stdout)) {
        const err = new Error(`${bin} exited ${code ?? `(${signal})`}${stderr ? `: ${stderr.trim().slice(0, 200)}` : ''}`)
        err.code = code
        return reject(err)
      }
      resolve({ stdout })
    })
  })
}

/** 自定义 agent 契约输出解析：整段 JSON（含 ok 字段）→ 对象；否则 null（回退 NDJSON 路径）。 */
export function parseAgentOutput(stdout) {
  try {
    const obj = JSON.parse(stdout)
    if (obj && typeof obj === 'object' && !Array.isArray(obj) && 'ok' in obj) return obj
  } catch {}
  return null
}

/** v1.8: 按 runner 构造子进程命令。agent → null（调用方走默认参数路径）；command → {bin, args}。
 *  契约：<agent_command> --prompt <完整提示词> --mode <mode>，stdout 输出
 *  {"ok":true,"text":...,"analysis"?:...,"parsed"?:...,"raw"?:...}（或 NDJSON 兼容）。
 *  mode: analysis|send|other（run）/ extract|verify|recall|replan（runSchedule）。
 *  #99：command 分支自动拼接 PERSONALITY/GUIDE/TOOLS 三段内容进 --prompt，
 *  与 agent 模式（--append-system-prompt 三段）行为一致，保证换后端不丢人格。 */
export function runnerCommand(mode, sysPrompt) {
  if (RUNNER !== 'command' || !AGENT_COMMAND.length) return null
  let prompt = sysPrompt
  if (RUNNER === 'command') {
    const parts = [PERSONALITY, GUIDE, TOOLS].map((p) => {
      try { return readFileSync(p, 'utf8') } catch { return '' }
    }).filter(Boolean)
    if (parts.length) prompt = `${parts.join('\n\n')}\n\n${sysPrompt}`
  }
  return {
    bin: AGENT_COMMAND[0],
    args: [...AGENT_COMMAND.slice(1), '--prompt', prompt, '--mode', mode],
  }
}

/** 共享 agent 参数构造(print 模式与 RPC 常驻复用):不含 -p/--mode/prompt。 */
export function buildBaseAgentArgs({ analysisMode = false, sessionId = SESSION_ID, noSkills = true } = {}) {
  return ['--provider', PROVIDER, '--model', MODEL,
    '--session-id', sessionId, '--no-context-files', ...(noSkills ? ['--no-skills'] : []),
    '--append-system-prompt', PERSONALITY,
    '--append-system-prompt', GUIDE,
    '--append-system-prompt', TOOLS,
    '--thinking', analysisMode ? REPLY_THINKING : THINKING]
}

/** send-mode 主动消息模板（print 与 RPC 共用）。 */
export function buildSendPrompt(decisionJson) {
  return `你是迟菓。以下是主动消息决策结果 JSON（action=send）。按迟菓人格与 context 中的 layer_guidance/instruction 生成 1-3 句微信消息发给哥哥，自然、不汇报、不打破第四面墙。\n\n决策：${decisionJson}`
}

/** analysis-mode 用户消息模板(print 与 RPC 共用)。 */
export function buildAnalysisPrompt(message) {
  return `你是迟菓。以下是当前收到的一条微信消息。先输出 JSON 情绪分析：{"warmth":-1~1,"effort":0~1,"attention":0~1,"topic":"可选","suppress_hours":"可选","recall":"可选(涉及登记事实/过去日期时给检索词,否则省略)","user_mood":"可选(calm|low|distressed|happy|angry)","user_mood_intensity":"可选(0~1)"}，用 <<ANALYSIS>>{...}<<END>> 包裹。然后以迟菓人格自然回复哥哥。\n\n消息：${message}`
}

export async function run(exec, { prompt, analysisMode, sendMode }) {
  let sysPrompt = prompt
  if (analysisMode) {
    sysPrompt = buildAnalysisPrompt(prompt)
  } else if (sendMode) {
    sysPrompt = buildSendPrompt(prompt)
  }
  const mode = analysisMode ? 'analysis' : sendMode ? 'send' : 'other'
  const custom = runnerCommand(mode, sysPrompt)
  const bin = custom ? custom.bin : AGENT_BIN
  const args = custom ? custom.args
    : ['-p', ...buildBaseAgentArgs({ analysisMode }), '--mode', 'json', sysPrompt]
  const t0 = Date.now()
  const tele = (ok, text, usage, error) => appendTelemetry({
    ts: new Date().toISOString(), mode, runner: RUNNER,
    dur_ms: Date.now() - t0, ok, text_len: text?.length ?? 0,
    usage: usage ?? null, error: error?.slice(0, 200) ?? null,
  })
  try {
    const { stdout } = await exec(bin, args, { timeout: AGENT_TIMEOUT, maxBuffer: 16 * 1024 * 1024 })
    let text = ''
    let analysis = null
    let usage = null
    const agentJson = custom ? parseAgentOutput(stdout) : null
    if (agentJson) {
      if (agentJson.ok === false) {
        tele(false, null, null, agentJson.error ?? 'agent error')
        return { ok: false, error: agentJson.error ?? 'agent error' }
      }
      text = agentJson.text ?? ''
      analysis = agentJson.analysis ?? null
      usage = agentJson.usage ?? null
      // 契约 JSON 未带 analysis 但文本含分析块 → 兼容剥离
      if (analysisMode && analysis == null) {
        const ex = extractAnalysis(text)
        analysis = ex.analysis
        text = ex.reply
      }
    } else {
      text = parseNdjson(stdout)
      usage = parseUsage(stdout)
      if (analysisMode) {
        const { analysis: an, reply } = extractAnalysis(text)
        analysis = an
        text = reply
      }
    }
    tele(!!text, text, usage)
    if (!text) return { ok: false, error: 'empty reply' }
    return analysisMode ? { ok: true, text, analysis } : { ok: true, text }
  } catch (err) {
    tele(false, null, null, err.message)
    return { ok: false, error: err.message }
  }
}

/** #99: agent 后端统一入口（bridge 只依赖本函数 + runSchedule，不 import 内部解析函数）。
 *  一次调用完成「情绪分析 JSON + 回复」（= run analysisMode 便捷封装）。
 *  exec 可注入（测试）；默认 runAgentBin。返回 {ok, text, analysis?} 或 {ok:false, error}。 */
export async function askAgent(exec = runAgentBin, prompt) {
  return run(exec, { prompt, analysisMode: true })
}

/** 写/回忆命令链路新模式:独立会话,知识边界(与聊天会话零共享)。提取/校验块解析,C7。 */
export async function runSchedule(exec, { mode, prompt, extra = {} }) {
  // 独立会话:extract/verify/recall/replan(与聊天会话零共享,知识边界)
  const SESSIONS = { extract: 'chiguo-extract', verify: 'chiguo-verify',
                     recall: 'chiguo-recall', replan: 'chiguo-replan' }
  const marker = mode === 'extract' ? 'EXTRACT' : mode === 'verify' ? 'VERIFY'
              : mode === 'recall' ? 'RECALL' : 'REPLAN'
  let sysPrompt = prompt
  if (mode === 'extract') {
    sysPrompt = `你是迟菓的安排提取器。今天是${extra.today}。把哥哥的话转成写命令 item JSON。
协议 item schema:{kind: cancel|move|add|exam_week|reminder|remove, when: 日期令牌,
period?, to_period?, to_date?, course?, label?, match?}。
日期令牌:显式日期 {date:"YYYY-MM-DD"} 或无年份 {date:"MM-DD"}(引擎补年份,不得自己算年份);
相对时间 {days:n}/{weekday:1-7}/{week_offset:0|1}/{week_offset:k,weekday:d}。
学期周次:第 ${extra.week_num} 周。
信息不足必须返回 {ok:false, question, missing},禁止填默认值;非安排命令返回 {ok:false, not_command:true}。
用 <<EXTRACT>>{...}<<END>> 包裹。\n\n消息：${prompt}`
  } else if (mode === 'verify') {
    sysPrompt = `你是迟菓的安排校验员。对照原文审查 item JSON 是否有无依据字段/自相矛盾/歧义。
通过输出 <<VERIFY>>{"ok":true}<<END>>;不过输出 <<VERIFY>>{"ok":false,"question":"追问文案","missing":["字段"]}<<END>>。\n\n原文：${prompt}\n\nitem：${extra.item}`
  } else if (mode === 'recall') {
    sysPrompt = `你是迟菓。依据检索事实回答哥哥的问题。只依据事实回答,禁止编造;检索无结果时反问用户('哥哥,那是什么时候呀?我帮你记上')。\n\n检索事实：${extra.facts}\n\n消息：${prompt}`
  }
  const custom = runnerCommand(mode, sysPrompt)
  const bin = custom ? custom.bin : AGENT_BIN
  const args = custom ? custom.args
    : ['-p', ...buildBaseAgentArgs({
        sessionId: SESSIONS[mode] || SESSION_ID, noSkills: false }),
      '--mode', 'json', sysPrompt]
  try {
    const { stdout } = await exec(bin, args, { timeout: AGENT_TIMEOUT, maxBuffer: 16 * 1024 * 1024 })
    let text = ''
    const agentJson = custom ? parseAgentOutput(stdout) : null
    if (agentJson) {
      if (agentJson.ok === false) return { ok: false, error: agentJson.error ?? 'agent error' }
      // 契约 JSON 直接带 parsed → 免块解析（自定义 agent 的最短路径）
      if (agentJson.parsed !== undefined) {
        return { ok: true, parsed: agentJson.parsed, raw: agentJson.raw ?? agentJson.text ?? '' }
      }
      text = agentJson.raw ?? agentJson.text ?? ''
    } else {
      text = parseNdjson(stdout)
    }
    if (!text) return { ok: false, error: 'empty reply' }
    const block = extractBlock(text, marker)
    if (!block) {
      // recall:<<RECALL>> 块可缺(回答即文本,§4.3 反问引导承担无匹配);extract/verify 必须出块
      if (mode === 'recall') return { ok: true, parsed: null, raw: text }
      return { ok: false, error: 'malformed block' }
    }
    try {
      return { ok: true, parsed: JSON.parse(block), raw: text }
    } catch {
      return { ok: false, error: 'block not json' }
    }
  } catch (err) {
    return { ok: false, error: err.message }
  }
}

async function main() {
  const args = process.argv.slice(2)
  const promptIdx = args.indexOf('--prompt')
  if (promptIdx < 0) { console.error('usage: agent-run.mjs --prompt <text> [--analysis-mode|--send-mode|--schedule-extract|--schedule-verify|--schedule-recall|--schedule-replan]'); process.exit(2) }
  const prompt = args[promptIdx + 1]
  if (args.includes('--schedule-extract')) {
    const attIdx = args.indexOf('--attention')
    let attention = {}
    try { attention = JSON.parse(attIdx >= 0 ? args[attIdx + 1] : '{}') } catch {}
    const wnIdx = args.indexOf('--week-num')
    const weekNum = wnIdx >= 0 ? args[wnIdx + 1] : String(attention.week_num ?? 1)
    const today = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10)  // CST 日期
    console.log(JSON.stringify(await runSchedule(runAgentBin, { mode: 'extract', prompt,
      extra: { today, attention: JSON.stringify(attention), week_num: weekNum } })))
    return
  }
  if (args.includes('--schedule-verify')) {
    const itemIdx = args.indexOf('--item')
    const item = itemIdx >= 0 ? args[itemIdx + 1] : '{}'
    console.log(JSON.stringify(await runSchedule(runAgentBin, { mode: 'verify', prompt, extra: { item } })))
    return
  }
  if (args.includes('--schedule-recall')) {
    const factsIdx = args.indexOf('--facts')
    const facts = factsIdx >= 0 ? args[factsIdx + 1] : '[]'
    console.log(JSON.stringify(await runSchedule(runAgentBin, { mode: 'recall', prompt, extra: { facts } })))
    return
  }
  if (args.includes('--schedule-replan')) {
    console.log(JSON.stringify(await runSchedule(runAgentBin, { mode: 'replan', prompt })))
    return
  }
  const analysisMode = args.includes('--analysis-mode')
  const sendMode = args.includes('--send-mode')
  console.log(JSON.stringify(await run(runAgentBin, { prompt, analysisMode, sendMode })))
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main()
