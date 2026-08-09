#!/usr/bin/env node
/**
 * agent-rpc.mjs — 常驻 agent 进程(RPC 模式)客户端,供 bridge 回复链与发送链复用。
 *
 * 架构动机(对比 OpenClaw 常驻 gateway):每次消息 spawn 新 pi 进程会重复
 * 扩展初始化(LanceDB 打开/FTS/健康检查)。RPC 常驻让 pi + 记忆扩展只初始化一次,
 * 每消息走 stdio JSON-RPC prompt,省去 per-message 进程/初始化开销。
 *
 * 协议(pi dist/modes/rpc):stdin 发 {type,id,...} 命令;stdout 流式输出
 * AgentSessionEvent(含 message_end 的 message.usage)+ {type:'response'} 响应。
 * prompt 命令的 response 在 preflight 即回;回合完成以 agent_settled 事件为准。
 *
 * 多会话(v1.11 B1):按 (mode, sessionId) 分片管理常驻进程——analysis 用
 * chiguo-main(回复链)、send 用 chiguo-send(主动发送链),进程彼此隔离、
 * 崩溃互不影响;每会话独立 pidfile 防孤儿(新实例启动时杀掉全部旧 pid)。
 *
 * 安全网:启动/调用任何失败 → 抛错,调用方(bridge)回退旧 spawn 路径;
 * 进程崩溃 → 下一轮 prompt 自动重启;bridge 退出 → 子进程随 stdin EOF 退出。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync, readFileSync, existsSync, unlinkSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import {
  buildBaseAgentArgs, buildAnalysisPrompt, buildSendPrompt,
  parseNdjson, extractAnalysis, parseUsage, appendTelemetry, AGENT_TIMEOUT,
} from '../scripts/agent-run.mjs'

const PID_DIR = join(homedir(), '.pi', 'agent')
const DEFAULT_SESSIONS = { analysis: 'chiguo-main', send: 'chiguo-send' }

export class AgentRpc {
  constructor({ bin = process.env.AGENT_BIN ?? 'pi', args = [] } = {}) {
    this.bin = bin
    this.args = args
    this.sessions = new Map()  // key `${mode}|${sessionId}` → {proc, buffer, pending, dead}
    // 干净 HOME（CI/新机器）无 ~/.pi/agent → 先建目录再写 pidfile，否则 writeFileSync ENOENT
    // mode 0o700：防宽松 umask 下他人可写目录 + _killStale 按 pid 杀进程的本地 DoS（security review）
    mkdirSync(PID_DIR, { recursive: true, mode: 0o700 })
    this._killStale()
  }

  // ── 会话管理 ─────────────────────────────────────────────

  _key(mode, sessionId) {
    return `${mode}|${sessionId ?? DEFAULT_SESSIONS[mode] ?? 'chiguo-main'}`
  }

  _get(key) {
    let s = this.sessions.get(key)
    if (!s) {
      s = { proc: null, buffer: [], pending: null, dead: true }
      this.sessions.set(key, s)
    }
    return s
  }

  _pidFile(key) {
    return join(PID_DIR, `agent-rpc-${key.replace(/[^a-z0-9-]/gi, '_')}.pid`)
  }

  /** 防孤儿:杀掉本实例之外的旧会话 pid(含改名前的 agent-rpc.pid 残留)。 */
  _killStale() {
    try {
      const files = readdirSync(PID_DIR).filter((f) => f.startsWith('agent-rpc'))
      for (const f of files) {
        try {
          const old = Number(readFileSync(join(PID_DIR, f), 'utf8').trim())
          if (old > 0 && old !== process.pid) {
            try { process.kill(old, 'SIGTERM') } catch {}
          }
          unlinkSync(join(PID_DIR, f))
        } catch {}
      }
    } catch {}
  }

  _cleanupPid(key) {
    try {
      const p = this._pidFile(key)
      if (existsSync(p)) unlinkSync(p)
    } catch {}
  }

  // ── 生命周期 ─────────────────────────────────────────────

  async ensureStarted({ mode = 'analysis', sessionId } = {}) {
    const key = this._key(mode, sessionId)
    const s = this._get(key)
    if (s.proc && !s.dead) return s
    s.dead = false
    s.buffer = []
    const finalSession = sessionId ?? DEFAULT_SESSIONS[mode] ?? 'chiguo-main'
    const args = [...this.args, '--mode', 'rpc',
      ...buildBaseAgentArgs({ analysisMode: mode === 'analysis', sessionId: finalSession })]
    const proc = spawn(this.bin, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    s.proc = proc
    writeFileSync(this._pidFile(key), String(proc.pid))
    // spawn 失败(bin 不存在等)或运行期异常 → 立即标记死亡并 reject ready/pending,不等 10s 超时
    let readyReject = null
    proc.on('error', (e) => {
      s.dead = true
      this._cleanupPid(key)
      const p = s.pending
      s.pending = null
      if (p) p.reject(e)
      if (readyReject) { const r = readyReject; readyReject = null; r(e) }
    })
    proc.stdout.on('data', (d) => {
      const lines = d.toString().split('\n')
      for (const line of lines) {
        if (!line.trim()) continue
        s.buffer.push(line)
        this._maybeSettle(key, line)
      }
    })
    proc.stderr.on('data', () => {})
    proc.on('exit', () => {
      s.dead = true
      this._cleanupPid(key)
      const p = s.pending
      s.pending = null
      if (p) p.reject(new Error('agent rpc 进程退出'))
    })
    // 等待 RPC 就绪:发 get_state 命令,收到 response 即确认(stdin/stdout 通路 + 会话已绑定)
    await new Promise((resolve, reject) => {
      readyReject = reject
      const stateId = `ready-${Date.now()}`
      const t = setTimeout(() => {
        // #review: 超时只 reject 会留下未就绪进程，下轮 prompt 短路复用 → 每轮空耗
        // 至 AGENT_TIMEOUT 才自愈。超时即杀进程 + 标记 dead + 清 pid，下轮自动重启。
        try { proc.kill('SIGTERM') } catch {}
        s.dead = true
        this._cleanupPid(key)
        proc.stdout.off('data', onLine)
        reject(new Error('agent rpc 启动超时'))
      }, 10_000)
      const onLine = (line) => {
        try {
          const ev = JSON.parse(line)
          if (ev.type === 'response' && ev.id === stateId) {
            clearTimeout(t)
            proc.stdout.off('data', onLine)
            resolve()
          }
        } catch {}
      }
      proc.stdout.on('data', onLine)
      proc.stdin.write(`${JSON.stringify({ type: 'get_state', id: stateId })}\n`)
    })
  }

  _maybeSettle(key, line) {
    const s = this.sessions.get(key)
    if (!s || !s.pending) return
    try {
      const ev = JSON.parse(line)
      if (ev.type === 'response' && ev.command === 'prompt' && ev.success === false) {
        const p = s.pending
        s.pending = null
        p.reject(new Error(ev.error ?? 'prompt preflight 失败'))
        return
      }
      // 回合完成仅以 agent_settled 为准（message_end 单独出现不代表回合结束）
      if (ev.type === 'agent_settled') {
        const p = s.pending
        s.pending = null
        p.resolve()
      }
    } catch {}
  }

  /** 发 prompt 并等待回合完成,返回 bridge 形状 {text, analysis}。失败抛错。 */
  async prompt(message, { mode = 'analysis', sessionId } = {}) {
    const key = this._key(mode, sessionId)
    const s = this.sessions.get(key) ?? this._get(key)
    const t0 = Date.now()
    await this.ensureStarted({ mode, sessionId })
    if (s.pending) throw new Error('RPC 已有进行中的 prompt(不应发生:bridge TurnQueue 串行)')
    const bufStart = s.buffer.length
    s.pending = { resolve: null, reject: null }
    const done = new Promise((res, rej) => {
      s.pending.resolve = res
      s.pending.reject = rej
    })
    const tpl = mode === 'send' ? buildSendPrompt(message) : buildAnalysisPrompt(message)
    s.proc.stdin.write(`${JSON.stringify({ type: 'prompt', message: tpl })}\n`)
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      const p = s.pending
      s.pending = null
      if (p) p.reject(new Error(`agent rpc 超时(${AGENT_TIMEOUT}ms)`))
    }, AGENT_TIMEOUT)
    try {
      await done
      const slice = s.buffer.slice(bufStart)
      const text = parseNdjson(slice.join('\n'))
      const usage = parseUsage(slice.join('\n'))
      appendTelemetry({
        ts: new Date().toISOString(), mode,
        dur_ms: Date.now() - t0, ok: !!text, text_len: text?.length ?? 0, usage: usage ?? null,
      })
      if (!text) throw new Error('RPC 空回复')
      const { analysis, reply } = extractAnalysis(text)
      return { text: reply, analysis }
    } finally {
      clearTimeout(timer)
      s.buffer = s.buffer.slice(bufStart)  // 本轮数据消费后裁剪,防 buffer 无界增长
      if (timedOut) this.restart({ mode, sessionId })
    }
  }

  /** /new 或 key 轮换后调用:杀指定会话(默认全部)进程,下一轮 prompt 自动重启。 */
  restart({ mode, sessionId } = {}) {
    const keys = mode ? [this._key(mode, sessionId)] : [...this.sessions.keys()]
    for (const key of keys) {
      const s = this.sessions.get(key)
      if (!s) continue
      try { s.proc?.kill('SIGTERM') } catch {}
      s.proc = null
      s.dead = true
      this._cleanupPid(key)
      const p = s.pending
      s.pending = null
      if (p) p.reject(new Error('agent rpc restart'))
    }
  }

  dispose() {
    this.restart()
  }
}

export default AgentRpc
