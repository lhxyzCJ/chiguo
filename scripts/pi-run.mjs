#!/usr/bin/env node
/**
 * pi-run — chiguo 的 pi-agent 调用统一封装。
 * 用法: node pi-run.mjs --prompt <文本> [--analysis-mode]
 * 配置: 环境变量或 toml [host] 段（PIRUN_* 覆盖）
 * 输出: {ok:true, text, analysis?} 或 {ok:false, error}
 */
import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
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
const PI_BIN = process.env.PI_BIN ?? 'pi'
const PROVIDER = process.env.PIRUN_PROVIDER ?? HOST.provider ?? 'opencode-go'  // provider 可配：pi --provider 名（内置或 models.json 自定义）
const MODEL = process.env.PIRUN_MODEL ?? HOST.model ?? 'deepseek-v4-flash'
const THINKING = process.env.PIRUN_THINKING ?? HOST.thinking_level ?? 'high'
const SESSION_ID = process.env.PIRUN_SESSION ?? HOST.session_id ?? 'chiguo-main'
const PERSONALITY_DIR = HOST.personality_dir ?? `${REPO}/personality`
const PERSONALITY = process.env.PIRUN_PERSONALITY ?? `${PERSONALITY_DIR}/SUN2.md`
const GUIDE = process.env.PIRUN_GUIDE ?? `${PERSONALITY_DIR}/迟菓语言技巧指南.md`

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

/** 用 spawn 收集 stdout（execFile 在 pi 下会挂起：pi 等 stdin EOF，execFile 管道不关） */
export function runPiBin(bin, args, opts) {
  return new Promise((resolve, reject) => {
    const c = spawn(bin, args, { stdio: ['ignore', 'pipe', 'pipe'], ...opts })
    let stdout = ''
    let stderr = ''
    c.stdout.on('data', (d) => { stdout += d })
    c.stderr.on('data', (d) => { stderr += d })
    c.on('error', (err) => reject(err))
    c.on('close', (code, signal) => {
      // 非零退出但 stdout 已含完整回复（如 teardown/session 保存失败）→ 不丢回复；
      // parseNdjson 取最后 message_end，无完整回复仍按失败处理
      if (code !== 0 && !parseNdjson(stdout)) {
        const err = new Error(`pi exited ${code ?? `(${signal})`}${stderr ? `: ${stderr.trim().slice(0, 200)}` : ''}`)
        err.code = code
        return reject(err)
      }
      resolve({ stdout })
    })
  })
}

export async function run(exec, { prompt, analysisMode, sendMode }) {
  let sysPrompt = prompt
  if (analysisMode) {
    sysPrompt = `你是迟菓。以下是当前收到的一条微信消息。先输出 JSON 情绪分析：{"warmth":-1~1,"effort":0~1,"attention":0~1,"topic":"可选","suppress_hours":"可选","recall":"可选(涉及登记事实/过去日期时给检索词,否则省略)"}，用 <<ANALYSIS>>{...}<<END>> 包裹。然后以 SUN2.md 人格自然回复哥哥。\n\n消息：${prompt}`
  } else if (sendMode) {
    sysPrompt = `你是迟菓。以下是主动消息决策结果 JSON（action=send）。按 SUN2.md 人格与 context 中的 layer_guidance/instruction 生成 1-3 句微信消息发给哥哥，自然、不汇报、不打破第四面墙。\n\n决策：${prompt}`
  }
  const piArgs = ['-p', '--provider', PROVIDER, '--model', MODEL,
    '--session-id', SESSION_ID, '--no-context-files',
    '--append-system-prompt', PERSONALITY,
    '--append-system-prompt', GUIDE,
    '--thinking', THINKING,
    '--mode', 'json', sysPrompt]
  try {
    const { stdout } = await exec(PI_BIN, piArgs, { timeout: 120_000, maxBuffer: 16 * 1024 * 1024 })
    const text = parseNdjson(stdout)
    if (!text) return { ok: false, error: 'empty reply' }
    if (analysisMode) {
      const { analysis, reply } = extractAnalysis(text)
      return { ok: true, text: reply, analysis }
    }
    return { ok: true, text }
  } catch (err) {
    return { ok: false, error: err.message }
  }
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
  const piArgs = ['-p', '--provider', PROVIDER, '--model', MODEL,
    '--session-id', SESSIONS[mode] || SESSION_ID, '--no-context-files',
    '--append-system-prompt', PERSONALITY, '--append-system-prompt', GUIDE,
    '--thinking', THINKING, '--mode', 'json', sysPrompt]
  try {
    const { stdout } = await exec(PI_BIN, piArgs, { timeout: 120_000, maxBuffer: 16 * 1024 * 1024 })
    const text = parseNdjson(stdout)
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
  if (promptIdx < 0) { console.error('usage: pi-run.mjs --prompt <text> [--analysis-mode|--send-mode|--schedule-extract|--schedule-verify|--schedule-recall|--schedule-replan]'); process.exit(2) }
  const prompt = args[promptIdx + 1]
  if (args.includes('--schedule-extract')) {
    const attIdx = args.indexOf('--attention')
    let attention = {}
    try { attention = JSON.parse(attIdx >= 0 ? args[attIdx + 1] : '{}') } catch {}
    const wnIdx = args.indexOf('--week-num')
    const weekNum = wnIdx >= 0 ? args[wnIdx + 1] : String(attention.week_num ?? 1)
    const today = new Date(Date.now() + 8 * 3600e3).toISOString().slice(0, 10)  // CST 日期
    console.log(JSON.stringify(await runSchedule(runPiBin, { mode: 'extract', prompt,
      extra: { today, attention: JSON.stringify(attention), week_num: weekNum } })))
    return
  }
  if (args.includes('--schedule-verify')) {
    const itemIdx = args.indexOf('--item')
    const item = itemIdx >= 0 ? args[itemIdx + 1] : '{}'
    console.log(JSON.stringify(await runSchedule(runPiBin, { mode: 'verify', prompt, extra: { item } })))
    return
  }
  if (args.includes('--schedule-recall')) {
    const factsIdx = args.indexOf('--facts')
    const facts = factsIdx >= 0 ? args[factsIdx + 1] : '[]'
    console.log(JSON.stringify(await runSchedule(runPiBin, { mode: 'recall', prompt, extra: { facts } })))
    return
  }
  if (args.includes('--schedule-replan')) {
    console.log(JSON.stringify(await runSchedule(runPiBin, { mode: 'replan', prompt })))
    return
  }
  const analysisMode = args.includes('--analysis-mode')
  const sendMode = args.includes('--send-mode')
  console.log(JSON.stringify(await run(runPiBin, { prompt, analysisMode, sendMode })))
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main()
