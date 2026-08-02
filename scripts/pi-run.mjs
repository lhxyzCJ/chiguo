#!/usr/bin/env node
/**
 * pi-run — chiguo 的 pi-agent 调用统一封装。
 * 用法: node pi-run.mjs --prompt <文本> [--analysis-mode]
 * 配置: 环境变量或 toml [host] 段（PIRUN_* 覆盖）
 * 输出: {ok:true, text, analysis?} 或 {ok:false, error}
 */
import { spawn } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

const REPO = process.env.CHIGUO_REPO ?? '/root/chiguo'
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
    sysPrompt = `你是迟菓。以下是当前收到的一条微信消息。先输出 JSON 情绪分析：{"warmth":-1~1,"effort":0~1,"attention":0~1,"topic":"可选","suppress_hours":"可选"}，用 <<ANALYSIS>>{...}<<END>> 包裹。然后以 SUN2.md 人格自然回复哥哥。\n\n消息：${prompt}`
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

async function main() {
  const args = process.argv.slice(2)
  const promptIdx = args.indexOf('--prompt')
  if (promptIdx < 0) { console.error('usage: pi-run.mjs --prompt <text> [--analysis-mode|--send-mode]'); process.exit(2) }
  const prompt = args[promptIdx + 1]
  const analysisMode = args.includes('--analysis-mode')
  const sendMode = args.includes('--send-mode')
  console.log(JSON.stringify(await run(runPiBin, { prompt, analysisMode, sendMode })))
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main()
