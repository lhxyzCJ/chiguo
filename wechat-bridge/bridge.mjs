#!/usr/bin/env node
/**
 * wechat-bridge — 微信 (wechatbot fork) ↔ pi-agent 桥接
 *
 * 微信消息 → pi-agent（scripts/agent-run.mjs，chiguo-main 会话）→ 回复发回微信。
 * 使用 fork 的 inboundDebounce 合并连发文本（windowMs 4000）。
 *
 * v2 新增（迟菓主动链路）:
 *  - 主动发送端点: POST http://127.0.0.1:18790/send {"to","text"} → bot.send()
 *    （agent 生成消息后 curl 调用；仅允许发给 OWNER_ID）
 *  - 回复确定性回传: 收到主人消息先跑 chiguo_daemon.py --user-msg（无分析），
 *    standing order 随后由 agent 补 --analysis（daemon recv_dedup 升级语义，不重复记账）。
 *
 * v3 可移植化（随 chiguo 仓库部署）:
 *  - storageDir 默认 = 本文件同目录 credentials/（仅本地保留，不进 git（隐私）；
 *    失效时 SDK 打印二维码重新扫码，即"尝试保留"）。绝不写入 wechatbot 仓库。
 *  - 所有路径/端口/主人 ID 可用 WECHAT_BRIDGE_* 环境变量覆盖（scripts/wechat-bridge.sh 生成 .env）。
 *
 * v4（Phase 4 寄主迁移）:
 *  - 回复侧由 pi-agent 完成情绪分析与回复：askAgent 调 scripts/agent-run.mjs
 *    （--prompt <原文> --analysis-mode），一次完成「情绪分析 JSON + 回复」。
 *  - 分析接线：askAgent 返回 analysis 后 → daemon --user-msg <原文> --analysis '<JSON>'
 *    （recv_dedup 升级语义——bridge 已确定性 --user-msg 过，不重复记账）。
 *  - 特殊命令（纪念日/假期）确定性接管：收到消息先 detectSpecialCommand（规则化，
 *    不依赖 agent 输出稳定性），命中 → 直接执行 daemon --anniversary/--break 并回复确认，
 *    不再经 agent（agent 为纯文本调用，无工具权限）。
 */
import { createServer } from 'node:http'
import { WeChatBot } from '@wechatbot/wechatbot'
import { spawn, execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { pathToFileURL } from 'node:url'
import { existsSync, readFileSync, writeFileSync, chmodSync, renameSync, rmSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { detectSpecialCommand, executeSpecialCommand, detectScheduleIntent, detectSlashCommand, executeSlashCommand } from './command-detect.mjs'
// #99 A 路：askAgent（agent-run.mjs 统一入口）由阶段 4 集成接入；当前保留原 spawn 调用结构
import { parseNdjson, extractAnalysis, resolveRepo, RUNNER } from '../scripts/agent-run.mjs'

const execFileP = promisify(execFile)

const DEBOUNCE_MS = 4000
const AGENT_RUN_SCRIPT = process.env.WECHAT_BRIDGE_AGENT_RUN
// RPC 常驻(仿 OpenClaw gateway):env WECHAT_BRIDGE_AGENT_RPC=1 显式启用;失败自动回退 spawn。
// v1.8: RPC 是 agent 二进制特有协议(--mode rpc)——runner=command(自定义 agent)时强制关闭。
const AGENT_RPC_ENABLED = RUNNER === 'agent' && process.env.WECHAT_BRIDGE_AGENT_RPC === '1'
const SEND_PORT = Number(process.env.WECHAT_BRIDGE_SEND_PORT ?? 18790)
// #84 /send 共享 token:未设置时跳过 token 校验(向后兼容 tick.sh 等既有调用);设置后必须匹配
const BRIDGE_TOKEN = process.env.WECHAT_BRIDGE_TOKEN
const OWNER_ID = process.env.WECHAT_BRIDGE_OWNER ?? 'owner@im.wechat'
// 仓库根 = 本文件位置推导（可移植，随仓库克隆到任何路径）
const REPO = resolveRepo(import.meta.url)
const DAEMON_PY = process.env.WECHAT_BRIDGE_DAEMON_PY ?? `${REPO}/.venv/bin/python`
const DAEMON_SCRIPT = process.env.WECHAT_BRIDGE_DAEMON ?? `${REPO}/chiguo_daemon.py`
// schedule 运行时文件锚定 daemon 所在目录（跟随 WECHAT_BRIDGE_DAEMON 覆盖；测试隔离依赖），与 REPO 仅默认相等
const REPO_ROOT = dirname(DAEMON_SCRIPT)
// agent 假死记账脚本（agent_health.py 状态机）；默认随仓库 scripts/ 部署，可用 WECHAT_BRIDGE_AGENT_HEALTH 覆盖
const AGENT_HEALTH_SCRIPT = process.env.WECHAT_BRIDGE_AGENT_HEALTH
  ?? new URL('../scripts/agent_health.py', import.meta.url).pathname
// agent_health 解释器独立于 DAEMON_PY（测试可能把后者换成 node 跑 fake daemon）
const AGENT_HEALTH_PY = process.env.WECHAT_BRIDGE_AGENT_HEALTH_PY ?? `${REPO}/.venv/bin/python`
// 登录态目录：默认仓库内回退；wechat-bridge.sh 注入集中认证目录 ~/.chiguo/auth/wechat（可迁移）；可用 WECHAT_BRIDGE_STORAGE 覆盖
const DEFAULT_STORAGE = new URL('./credentials/', import.meta.url).pathname

/** 串行化 agent 调用（同一 agent 会话 chiguo-main 不允许并发 turn，含 chiguo-tick 的周期调用）。 */
export class TurnQueue {
  constructor() {
    this.tail = Promise.resolve()
  }

  run(task) {
    const next = this.tail.then(task, task)
    this.tail = next.catch(() => {})
    return next
  }
}

/** 调用 pi-agent（agent-run.mjs），一次完成「情绪分析 JSON + 回复」。
 * 返回 { text, analysis }；analysis 为解析后的对象或 null。失败抛错。 */
export async function askAgent(text) {
  // RPC 常驻优先:失败 → 回退 spawn(agent-rpc 抛错即回退)
  if (AGENT_RPC_ENABLED) {
    try {
      const { AgentRpc } = await import('./agent-rpc.mjs')
      if (!globalThis.__agentRpc) globalThis.__agentRpc = new AgentRpc()
      const r = await globalThis.__agentRpc.prompt(text)
      return { text: r.text, analysis: r.analysis ?? null }
    } catch (err) {
      console.error('[agent-rpc] 失败,回退 spawn:', err instanceof Error ? err.message : String(err))
    }
  }
  const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, '--prompt', text, '--analysis-mode'], {
    timeout: 180_000,
    maxBuffer: 16 * 1024 * 1024,
  })

  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch {
    throw new Error(`agent-run 输出非 JSON: ${String(stdout).slice(0, 100)}`)
  }
  if (!parsed.ok) {
    throw new Error(parsed.error ?? 'agent-run 返回 ok=false 且无 error')
  }
  return { text: parsed.text, analysis: parsed.analysis ?? null }
}

// ── 6b:recall 信号 + 回复侧 --attention 注入(导出供测试注入 fake run)──
// #84 单通道:事实只走 --facts 参数(agent-run recall 模板自行拼装并含反问引导),prompt 不放事实。

/** --attention 轻量读(§5.4):失败/畸形 → null(跳过注入继续 askAgent,不阻塞回复流)。 */
async function getAttention() {
  try {
    const r = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--attention'], {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
    })
    return JSON.parse(r.stdout)
  } catch {
    return null
  }
}

/** T1/T2/T3 + today_exceptions 拼成回复侧注入块(§5.4 同源组装)。 */
function buildAttentionBlock(att) {
  const a = att?.attention ?? {}
  const lines = []
  if (Array.isArray(a.t1) && a.t1.length) {
    lines.push(`重要日子:${a.t1.map((x) => `${x.date} ${x.name ?? x.label ?? ''}(还有${x.days_until}天)`).join('、')}`)
  }
  if (Array.isArray(a.t2) && a.t2.length) {
    lines.push(`区间事实:${a.t2.join(';')}`)
  }
  const w = a.t3?.this_week
  if (w && Object.keys(w).length) {
    const days = Object.entries(w)
      .map(([d, periods]) => `${d}日:${Object.entries(periods).map(([p, c]) => `${p}节${c?.course ?? c}`).join(',')}`)
      .join(';')
    lines.push(`本周课表:${days}`)
  }
  if (Array.isArray(a.today_exceptions) && a.today_exceptions.length) {
    lines.push(`今日课程例外:${a.today_exceptions.map((e) => `${e.period}节${e.action}${e.course ? ` ${e.course}` : ''}`).join(';')}`)
  }
  return lines.join('\n')
}

/** askAgent 前先注入 attention 块(取数失败 → 原文直走,§5.4 降级)。 */
async function askAgentWithAttention(text, att) {
  const block = att?.ok ? buildAttentionBlock(att) : null
  const prompt = block ? `${text}\n\n【今日安排参考】\n${block}\n(仅供回答参考,仅在相关时提及)` : text
  return askAgent(prompt)
}

/** 第一趟分析(analysis-mode,含 recall 信号):默认走 askAgent;测试注入 {exec} fake(原始 ndjson 解析)。 */
async function firstAnalysis(text, runOverride) {
  if (typeof runOverride === 'function') return runOverride(text)
  const { stdout } = await runOverride.exec('node',
    [AGENT_RUN_SCRIPT, '--prompt', text, '--analysis-mode'], { timeout: 180_000, maxBuffer: 16 * 1024 * 1024 })
  const { analysis, reply } = extractAnalysis(parseNdjson(stdout))
  return { text: reply, analysis }
}

/** 第二趟 agent(recall 模式):事实经 --facts 参数传给 agent-run(其 recall 模板拼装事实+反问引导),
 * prompt 只放用户问题(#84 单通道,防 facts='[]' 覆盖真实事实)。
 * 返回 { text } 或 null(失败/漏检 → 调用方按普通回复,零额外调用)。 */
async function runAgentRun({ mode, prompt, facts }, runOverride = null) {
  const args = ['--prompt', prompt, '--schedule-recall', '--facts', facts]
  if (runOverride && typeof runOverride.exec === 'function') {
    const { stdout } = await runOverride.exec('node', [AGENT_RUN_SCRIPT, ...args],
      { timeout: 180_000, maxBuffer: 16 * 1024 * 1024 })
    const raw = parseNdjson(stdout)
    return { text: raw ? raw.replace(/<<RECALL>>[\s\S]*?<<END>>/, '').trim() : null }
  }
  const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, ...args], {
    timeout: 180_000,
    maxBuffer: 16 * 1024 * 1024,
  })
  let out
  try { out = JSON.parse(stdout) } catch { return null }
  if (!out?.ok) return null
  const raw = out.raw ?? out.text ?? ''
  return { text: raw.replace(/<<RECALL>>[\s\S]*?<<END>>/, '').trim() }
}

/** recall 信号路由:analysis 带 recall → daemon --schedule-recall → 第二趟 agent → 回答;
 * 无信号/失败/漏检 → null(调用方按普通回复,零额外调用)。 */
export async function runWithRecall(text, runOverride = askAgent) {
  const first = await firstAnalysis(text, runOverride)
  if (first?.analysis?.recall) {
    const r = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--schedule-recall', first.analysis.recall], {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
    })
    let rec = null
    try { rec = JSON.parse(r.stdout) } catch {}
    if (rec?.ok) {
      const second = await runAgentRun({ mode: 'recall', prompt: text, facts: JSON.stringify(rec.matches) }, runOverride)
      if (second) return second.text
    }
  }
  return null
}

/** 回复侧注入(§5.4):先取 --attention(失败降级),成功注入 T1/T2/T3 + today_exceptions 再 askAgent。 */
export async function runWithAttention(text, runOverride = null) {
  const att = await getAttention()
  if (runOverride) {
    const res = await runOverride({ attention: att?.ok ? att : null, text })
    return typeof res === 'string' ? res : res?.text ?? null
  }
  return (await askAgentWithAttention(text, att)).text
}

/** 确定性记录主人消息到迟菓 daemon（无分析；随后的 askAgent 分析经 upgradeAnalysis 升级，daemon 去重）。失败不阻塞回复流。 */
export async function recordUserMsg(text) {
  try {
    await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--user-msg', text], {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
    })
  } catch (err) {
    console.error('[user-msg record error]',
      err instanceof Error ? err.message : String(err))
  }
}

/** 分析升级：askAgent 已产出情绪分析 JSON → daemon --user-msg --analysis。
 * recv_dedup 升级语义：同一原文（30s 窗口内，RECV_DEDUP_WINDOW_S）只补分析微调，不重复记账。失败不阻塞回复流。 */
export async function upgradeAnalysis(text, analysis) {
  if (!analysis) return
  const analysisJson = typeof analysis === 'string' ? analysis : JSON.stringify(analysis)
  try {
    await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--user-msg', text, '--analysis', analysisJson], {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
    })
  } catch (err) {
    console.error('[analysis upgrade error]',
      err instanceof Error ? err.message : String(err))
  }
}

/** agent 假死记账：askAgent/agent-run 成败记录进 agent_health 状态机（零额外 agent 调用）。
 * transition=down/up 时向 OWNER_ID 发告警/恢复消息。整体绝不抛错、绝不影响回复流。 */
export async function recordAgentHealth(bot, outcome, reason = null) {
  try {
    const args = [AGENT_HEALTH_SCRIPT, 'record', '--outcome', outcome]
    if (reason) args.push('--reason', String(reason).slice(0, 100))
    const { stdout } = await execFileP(AGENT_HEALTH_PY, args, {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
    })
    const parsed = JSON.parse(stdout)
    if (parsed.transition !== 'none' && parsed.message) {
      await bot.send(OWNER_ID, parsed.message)
        .catch((e) => console.error('[agent health alert send error]',
          e instanceof Error ? e.message : String(e)))
    }
    return parsed
  } catch (err) {
    console.error('[agent health record error]',
      err instanceof Error ? err.message : String(err))
    return null
  }
}

// ── schedule-center 6a:澄清记录(仓库根锚定 = DAEMON_SCRIPT 父目录;A3)──
export function scheduleClarifyPath(repoRoot = REPO_ROOT) { return join(repoRoot, 'schedule_clarify.json') }
export function readClarify(repoRoot = REPO_ROOT) {
  const p = scheduleClarifyPath(repoRoot)
  if (!existsSync(p)) return null
  try {
    const rec = JSON.parse(readFileSync(p, 'utf8'))
    if (!rec.expires_at || Date.parse(rec.expires_at) <= Date.now()) {  // 6h 过期静默清理
      rmSync(p, { force: true })
      return null
    }
    return rec
  } catch {  // 损坏 → 清空为无记录
    rmSync(p, { force: true })
    return null
  }
}
export function writeClarify(repoRoot, rec) {
  const p = scheduleClarifyPath(repoRoot)
  const tmp = p + '.tmp'
  writeFileSync(tmp, JSON.stringify(rec), { mode: 0o600 })
  chmodSync(tmp, 0o600)
  renameSync(tmp, p)
}
export function clearClarify(repoRoot = REPO_ROOT) { rmSync(scheduleClarifyPath(repoRoot), { force: true }) }
export function exitWordMatch(text) { return /^(?:算了|不要了|没事)/.test(text.trim()) }

/** /send 来源校验:Host/Origin 必须本地回环(127.0.0.1/localhost/::1),容忍端口后缀。 */
export function isLocalHost(host) {
  if (!host) return false
  const h = String(host).toLowerCase().replace(/:\d+$/, '')
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]'
}
export function isLocalOrigin(origin) {
  if (!origin) return true  // curl 等无 Origin 客户端:靠 Host + token 把关
  try {
    const u = new URL(origin)
    return u.protocol === 'http:' && isLocalHost(u.host)
  } catch { return false }
}

/** /agent/prompt 处理器（导出供测试）：经常驻 AgentRpc 生成回复。
 *  返回 {ok:true,text,analysis?}；失败抛 503（调用方回退 spawn）。 */
export async function handleAgentPrompt(payload, res) {
  const deny = (status, error) => {
    res.writeHead(status, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error }))
  }
  const { text, mode } = payload ?? {}
  if (typeof text !== 'string' || !text.trim()) { deny(400, 'text 必填'); return }
  if (mode !== undefined && !['analysis', 'send'].includes(mode)) {
    deny(400, 'mode 必须是 analysis|send')
    return
  }
  try {
    const { AgentRpc } = await import('./agent-rpc.mjs')
    if (!globalThis.__agentRpc) globalThis.__agentRpc = new AgentRpc()
    const r = await withTimeout(globalThis.__agentRpc.prompt(text, { mode: mode ?? 'analysis' }), 180_000)
    res.writeHead(200, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: true, text: r.text, analysis: r.analysis ?? null }))
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    console.error('[agent/prompt error]', reason)
    res.writeHead(503, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: reason }))
  }
}

/** 主动发送端点：POST /send {"to","text"} → bot.send()；POST /agent/prompt {"text","mode"} → AgentRpc。
 *  仅本地回环来源(#84 鉴权)+ 可选 token。 */
function startSendServer(bot) {
  const server = createServer((req, res) => {
    const deny = (status, error) => {
      res.writeHead(status, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error }))
    }
    if (req.method !== 'POST' || (req.url !== '/send' && req.url !== '/agent/prompt')) {
      deny(405, 'only POST /send or /agent/prompt')
      return
    }
    // #84 鉴权:Content-Type JSON → 本地来源(Host/Origin) → 共享 token(未配置跳过)
    if (!String(req.headers['content-type'] ?? '').toLowerCase().includes('application/json')) {
      deny(415, 'Content-Type must be application/json')
      return
    }
    if (!isLocalOrigin(req.headers.origin)) { deny(403, 'forbidden origin'); return }
    if (!isLocalHost(req.headers.host)) { deny(403, 'forbidden host'); return }
    if (BRIDGE_TOKEN && req.headers['x-bridge-token'] !== BRIDGE_TOKEN) {
      deny(403, 'forbidden token')
      return
    }
    let body = ''
    req.on('data', (c) => {
      body += c
      if (body.length > 1_000_000) {
        deny(413, 'payload too large')
        req.destroy()
      }
    })
    req.on('error', () => {})  // destroy 后连接重置，避免未处理错误事件
    req.on('end', async () => {
      // #84 参数校验:非法 JSON / 必填缺失 → 400(而非 500)
      let payload
      try { payload = JSON.parse(body || '{}') } catch { deny(400, 'invalid JSON'); return }
      if (req.url === '/agent/prompt') {
        await handleAgentPrompt(payload, res)
        return
      }
      const { to, text } = payload
      if (typeof to !== 'string' || !to.trim()) { deny(400, 'to 必填'); return }
      if (typeof text !== 'string' || !text.trim()) { deny(400, 'text 必填'); return }
      if (to !== OWNER_ID) { deny(403, 'forbidden recipient'); return }
      try {
        await withTimeout(bot.send(to, text), 30_000)   // #84 send 超时兜底,不挂死请求
        console.log(`[send] ${to}: ${text.slice(0, 80)}`)
        res.writeHead(200, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ ok: true }))
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err)
        console.error('[send error]', reason)
        res.writeHead(500, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: reason }))
      }
    })
  })
  // #84: listen 失败(端口占用等)→ 打印并退出,不留僵尸服务
  server.on('error', (err) => {
    console.error('[send server error]', err instanceof Error ? err.message : String(err))
    process.exit(1)
  })
  server.listen(SEND_PORT, '127.0.0.1', () => {
    console.log(`send server: http://127.0.0.1:${SEND_PORT}/send`)
  })
}

/** 命令链路默认实现:agent-run 模式 + daemon CLI(独立会话 chiguo-extract/verify;A4 shape 直读 stdout JSON) */
function makeScheduleDeps(repoRoot) {
  return {
    async extractAgent(original) {
      const att = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--attention'], { timeout: 30_000 }).catch(() => ({ stdout: '{}' }))
      let attention = {}
      try { attention = JSON.parse(att.stdout) } catch {}
      const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, '--prompt', original,
        '--schedule-extract', '--attention', JSON.stringify(attention), '--week-num', String(attention.week_num ?? 1)],
        { timeout: 180_000 })
      const res = JSON.parse(stdout)
      return res.parsed ?? { ok: false, error: 'no block' }
    },
    async verifyAgent(item, original) {
      const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, '--prompt', original,
        '--schedule-verify', '--item', JSON.stringify(item)], { timeout: 180_000 })
      return JSON.parse(stdout).parsed ?? { ok: false }
    },
    async runDaemon(item) {
      const { stdout } = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--schedule-change', JSON.stringify(item)],
        { timeout: 30_000 })
      return JSON.parse(stdout)
    },
  }
}

let scheduleDeps = null
function scheduleDefaults() {
  scheduleDeps ??= makeScheduleDeps(REPO_ROOT)
  return scheduleDeps
}
const defaultExtractAgent = (original) => scheduleDefaults().extractAgent(original)
const defaultVerifyAgent = (item, original) => scheduleDefaults().verifyAgent(item, original)
const defaultRunDaemon = (item) => scheduleDefaults().runDaemon(item)

/** 命令链路:提取 → 校验 → daemon 写入 → 确认/追问/澄清记录(180s 超时不阻塞队列,M16) */
async function handleScheduleCommand(original, msg, bot, deps) {
  const repoRoot = deps.repoRoot ?? REPO_ROOT
  try {
    const ex = await withTimeout(deps.extractAgent(original), 180_000)
    if (!ex.ok) {
      if (ex.not_command) return 'chat'                 // ⑤/⑥ 释放回聊天链(记录保留)
      const rec = { original, missing: ex.missing ?? [], question: ex.question ?? '哥哥没太听明白,再告诉哥哥一次具体安排?',
                    created_at: deps.now().toISOString(), expires_at: new Date(deps.now().getTime() + 6 * 3600e3).toISOString() }
      writeClarify(repoRoot, rec)
      await bot.reply(msg, rec.question)
      return 'clarify'
    }
    const vf = await withTimeout(deps.verifyAgent(ex.item, original), 180_000)
    if (!vf.ok) {
      const rec = { original, missing: vf.missing ?? [], question: vf.question ?? '这个安排有点对不上,哥哥再确认一下?',
                    created_at: deps.now().toISOString(), expires_at: new Date(deps.now().getTime() + 6 * 3600e3).toISOString() }
      writeClarify(repoRoot, rec)
      await bot.reply(msg, rec.question)
      return 'clarify'
    }
    const r = await withTimeout(deps.runDaemon(ex.item), 30_000)
    if (r.ok) {
      clearClarify(repoRoot)                            // ⑪ 成功写入后清记录(F4)
      await bot.reply(msg, r.text)
      return 'done'
    }
    // ⑩ 确定性拒绝(reason/question/missing 由 daemon 给出)→ 入澄清记录
    writeClarify(repoRoot, { original, missing: r.missing ?? [], question: r.question ?? '处理失败,再试一次?',
                             created_at: deps.now().toISOString(), expires_at: new Date(deps.now().getTime() + 6 * 3600e3).toISOString() })
    await bot.reply(msg, r.question)
    return 'rejected'
  } catch (err) {
    await bot.reply(msg, '⚠️ 处理失败,再试一次?').catch(() => {})   // 超时/异常兜底(M16/C7)
    return 'error'
  }
}

async function withTimeout(p, ms) {
  let timer
  try {
    return await Promise.race([
      p,
      new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`timeout ${ms}ms`)), ms) }),
    ])
  } finally { clearTimeout(timer) }
}

/** 聊天链回复:queue 串行 + askAgent;失败回通用文案(非本人/chat 放行共用,不回内部诊断)。 */
async function askChat(text, msg, bot, queue, askAgentFn) {
  await queue.run(async () => {
    try {
      const { text: reply } = await askAgentFn(text)
      await bot.reply(msg, reply).catch(() => {})
    } catch (err) {
      await bot.reply(msg, '⚠️ 处理失败').catch(() => {})   // 不回内部诊断(安全补钉)
      await recordAgentHealth(bot, 'fail', err.message)
    }
  })
}

/** 单条微信消息处理链路（onMessage 委托；导出供测试）：
 * 路由顺序:OWNER_ID 门(最顶部,C1) → recordUserMsg(仅本人) → 澄清检查 → detectSpecialCommand
 * → detectScheduleIntent → askAgent;命令消息经 --user-msg 无分析(recordUserMsg 已先行,dedup 30s 继承);
 * 非本人 = 仅 askAgent 回复 + 失败回通用文案(不回内部诊断,安全补钉)。
 * bot 需提供 reply(msg, text)/sendTyping(userId)；queue 提供 run(task)。 */
export async function handleMessage(text, msg, bot, queue, deps = {}) {
  if (!text?.trim()) return null
  const isOwner = msg.userId === OWNER_ID
  const repoRoot = deps.repoRoot ?? REPO_ROOT
  const askAgentFn = deps.askAgent ?? askAgent
  const extractAgent = deps.extractAgent ?? defaultExtractAgent
  const verifyAgent = deps.verifyAgent ?? defaultVerifyAgent
  const runDaemon = deps.runDaemon ?? defaultRunDaemon
  const now = deps.now ?? (() => new Date())

  // ── C1 门:非本人不进写/回忆/追问/特殊命令路径,不取 --attention;仅 askAgent 回复 ──
  if (!isOwner) {
    await askChat(text, msg, bot, queue, askAgentFn)
    return 'agent'
  }

  await recordUserMsg(text)   // 确定性回传 daemon(命令消息 = --user-msg 无分析,dedup 30s 继承)

  // 澄清检查:有待澄清记录且非退出词 → 路由回提取(词表命中=新命令;否则合并"原意+回答")
  const clarify = readClarify(repoRoot)
  let exitWord = false
  if (clarify && !exitWordMatch(text)) {
    const wordIntent = detectScheduleIntent(text)          // 词表命中 = 新命令
    const isNewCommand = wordIntent && wordIntent.intent !== 'extract'
    const original = isNewCommand ? text : `${clarify.original}\n(补充回答:${text})`
    const released = await handleScheduleCommand(original, msg, bot,
      { ...deps, repoRoot, clarify, extractAgent, verifyAgent, runDaemon, now })
    if (released !== 'chat') return released
    await askChat(text, msg, bot, queue, askAgentFn)   // ⑥ 放行回聊天链:消息必须获得回复(不静默丢弃)
    return 'chat'
  }
  if (clarify && exitWordMatch(text)) {
    clearClarify(repoRoot)
    exitWord = true   // 退出词:清记录后本消息不再进命令路径(短消息兜底不再重写记录)
  }

  // 微信端斜杠命令（白名单制）：全部 / 开头消息确定性接管，不经 agent/daemon
  const slash = detectSlashCommand(text)
  if (slash) {
    await queue.run(async () => {
      try {
        const r = await executeSlashCommand(spawn, slash, process.cwd())
        // /new 后常驻 agent 仍持有旧会话句柄 → 重启,下一轮 prompt 重载最新 chiguo-main
        if (slash.action === 'new_session' && AGENT_RPC_ENABLED && globalThis.__agentRpc) {
          globalThis.__agentRpc.restart()
          console.log('[slash] agent-rpc 已重启(新会话)')
        }
        console.log(`[slash] ${slash.action} → ok=${r.ok}`)
        await bot.reply(msg, r.reply)
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err)
        console.error('[slash error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason.slice(0, 100)}`).catch(() => {})
      }
    })
    return 'slash'
  }

  // 特殊命令（纪念日/假期）确定性接管：命中则直接执行 daemon，不经 agent（Phase 4 Task 14）
  const special = detectSpecialCommand(text)
  if (special) {
    await queue.run(async () => {
      try {
        const r = await executeSpecialCommand(spawn, special, DAEMON_PY, DAEMON_SCRIPT)
        console.log(`[special] ${special.daemon.join(' ')} → ok=${r.ok}`)
        await bot.reply(msg, r.reply)
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err)
        console.error('[special error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason.slice(0, 100)}`).catch(() => {})
      }
    })
    return 'special'
  }

  // 写/回忆命令意图 → 命令链路(提取 → 校验 → daemon;180s 超时不阻塞队列)
  // 返回值透传 handleScheduleCommand(not_command → 'chat' 释放回聊天链,⑤)
  const intent = exitWord ? null : detectScheduleIntent(text)
  if (intent) {
    const released = await queue.run(() => handleScheduleCommand(text, msg, bot,
      { ...deps, repoRoot, clarify: null, extractAgent, verifyAgent, runDaemon, now }))
    if (released !== 'chat') return released
    await askChat(text, msg, bot, queue, askAgentFn)   // ⑤ 误命中释放:消息必须获得回复(不静默丢弃)
    return 'chat'
  }

  try {
    await bot.sendTyping(msg.userId).catch(() => {})
  } catch {}

  await queue
    .run(async () => {
      let recalled = false
      try {
        // 回复侧:先取 --attention 注入(失败降级,§5.4),analysis 带 recall 信号 → 第二趟 agent
        const att = await getAttention()
        const { text: reply, analysis } = await askAgentWithAttention(text, att)
        if (analysis?.recall) {
          const second = await runWithRecall(text)
          if (second) {
            await bot.reply(msg, second).catch((e) => console.error('[reply error]', e))
            recalled = true
          }
        }
        if (!recalled) {
          await upgradeAnalysis(text, analysis)  // recv_dedup 升级语义，不重复记账
          console.log(`[out] ${reply.slice(0, 80)}`)
          await bot.reply(msg, reply).catch((e) => console.error('[reply error]', e))
        }
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err)
        console.error('[agent error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason.slice(0, 100)}`).catch(() => {})
        await recordAgentHealth(bot, 'fail', reason)
        return
      }
      // 回复失败不记 agent 假死（发送故障 ≠ agent 故障）；记账在回复后执行，不延迟当前消息
      await recordAgentHealth(bot, 'success')
    })
    .catch((err) => console.error('[queue error]', err))
  return 'agent'
}

async function main() {
  const bot = new WeChatBot({
    storage: 'file',
    storageDir: process.env.WECHAT_BRIDGE_STORAGE ?? DEFAULT_STORAGE,
    logLevel: 'info',
    inboundDebounce: {
      windowMs: DEBOUNCE_MS,
      joinSeparator: '\n',
    },
    loginCallbacks: {
      onQrUrl: (url) => {
        console.log('\n=== 微信扫码登录 ===')
        console.log(url)
        console.log('====================\n')
      },
      onScanned: () => console.log('已扫码，等待确认…'),
      onExpired: () => console.log('二维码已过期，刷新中…'),
    },
  })

  const queue = new TurnQueue()

  bot.onMessage(async (msg) => {
    const text = msg.text
    if (!text?.trim()) return
    console.log(`[in] ${msg.userId}: ${text.slice(0, 80)}`)
    await handleMessage(text, msg, bot, queue, makeScheduleDeps(REPO_ROOT))
  })

  bot.on('error', (err) => {
    console.error('[bot error]', err instanceof Error ? err.message : String(err))
  })
  bot.on('session:expired', () => console.warn('[bot] 会话过期，尝试重登…'))

  await bot.login()
  // 该 fork 的 bot.start() 长轮询挂起不返回 → 主动发送端点必须先于 start 就绪
  startSendServer(bot)
  await bot.start()
  console.log('wechat-bridge 运行中（Ctrl+C 停止）')
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error('启动失败:', err instanceof Error ? err.message : String(err))
    process.exit(1)
  })
}
