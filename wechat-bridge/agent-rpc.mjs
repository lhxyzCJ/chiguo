#!/usr/bin/env node
/**
 * agent-rpc.mjs — 常驻 agent 进程(RPC 模式)客户端,供 bridge 回复链与发送链复用。
 *
 * 架构动机(对比 OpenClaw 常驻 gateway):每次消息 spawn 新 pi 进程会重复
 * 进程/会话初始化。RPC 常驻让 pi 进程只初始化一次,
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
// R1: 单行(无 \n 残段)累积上限——异常 pi 打出无换行巨型行时防 lineBuffer 无限增长 OOM
const MAX_LINE_BUFFER = 1024 * 1024

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
      s = { proc: null, buffer: [], lineBuffer: '', lineHandlers: [], pending: null, dead: true }
      this.sessions.set(key, s)
    }
    return s
  }

  _pidFile(key) {
    return join(PID_DIR, `agent-rpc-${key.replace(/[^a-z0-9-]/gi, '_')}.pid`)
  }

  /** 防孤儿:杀掉本实例之外的旧会话 pid(含改名前的 agent-rpc.pid 残留)。
   *  A2:pid 复用防护——杀前确认该 pid 确是 rpc 进程(Linux 读 /proc cmdline 含 '--mode' 'rpc'),
   *  防止 pid 复用误杀同 uid 无关进程;非 Linux 平台回退仅存活检查。 */
  _killStale() {
    try {
      const files = readdirSync(PID_DIR).filter((f) => f.startsWith('agent-rpc'))
      for (const f of files) {
        try {
          const old = Number(readFileSync(join(PID_DIR, f), 'utf8').trim())
          if (old > 0 && old !== process.pid && this._isOurRpcProcess(old)) {
            try { process.kill(old, 'SIGTERM') } catch {}
          }
          unlinkSync(join(PID_DIR, f))
        } catch {}
      }
    } catch {}
  }

  /** pid 复用防护:kill(pid,0) 存活后读 /proc/<pid>/cmdline 确认是 rpc 进程才认可;非 Linux 回退现有行为。
   *  R2: 按 NUL 分割成 argv 精确匹配「独立 token '--mode' 且下一 token 恰为 'rpc'」——
   *  子串匹配会误杀含 '--mode' 子串的 '--model' 参数或参数值含 'rpc' 的无关进程。 */
  _isOurRpcProcess(pid) {
    try {
      process.kill(pid, 0)  // 存活检查(ESRCH = 不存在/无权限)
      if (process.platform !== 'linux') return true
      const argv = readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0')
      const modeIdx = argv.indexOf('--mode')
      return modeIdx >= 0 && argv[modeIdx + 1] === 'rpc'
    } catch {
      return false
    }
  }

  _cleanupPid(key) {
    try {
      const p = this._pidFile(key)
      if (existsSync(p)) unlinkSync(p)
    } catch {}
  }

  /** C1: 仅当 pidfile 内容仍指向该 proc 时才清理——restart 后旧进程延迟 exit
   *  不得删掉新进程写入的 pidfile(pidfile 按 key 存,新旧进程共用同一路径)。 */
  _cleanupPidIfOurs(key, proc) {
    try {
      const p = this._pidFile(key)
      if (existsSync(p) && Number(readFileSync(p, 'utf8').trim()) === proc.pid) unlinkSync(p)
    } catch {}
  }

  /** stdout 行处理:data chunk 边界任意,残段保留跨 chunk,只消费完整行(A1 修复)。
   *  R1: 残段无上限(异常 pi 无换行巨型行)→ 超限丢弃残段并重启该会话,防 OOM。 */
  _onChunk(key, chunk) {
    const s = this.sessions.get(key)
    if (!s) return
    s.lineBuffer += chunk
    let nl
    while ((nl = s.lineBuffer.indexOf('\n')) >= 0) {
      const line = s.lineBuffer.slice(0, nl)
      s.lineBuffer = s.lineBuffer.slice(nl + 1)
      this._handleLine(key, line)
    }
    if (s.lineBuffer.length > MAX_LINE_BUFFER) {
      console.error(`[agent-rpc] 行缓冲超限(${MAX_LINE_BUFFER} bytes),丢弃残段并重启会话 ${key}`)
      s.lineBuffer = ''
      this.restart({ key })
    }
  }

  /** 完整行分发:入 buffer + settle 判定 + 握手 handler(ready 判定)。 */
  _handleLine(key, line) {
    const s = this.sessions.get(key)
    if (!s) return
    const trimmed = line.trim()
    if (!trimmed) return
    s.buffer.push(trimmed)
    this._maybeSettle(key, trimmed)
    for (const h of s.lineHandlers) {
      try { h(trimmed) } catch {}
    }
  }

  // ── 生命周期 ─────────────────────────────────────────────

  async ensureStarted({ mode = 'analysis', sessionId } = {}) {
    const key = this._key(mode, sessionId)
    const s = this._get(key)
    if (s.proc && !s.dead) return s
    s.dead = false
    s.buffer = []
    s.lineBuffer = ''          // A1: 重启后旧残段作废
    s.lineHandlers = []        // A1: 旧握手 handler 作废(超时/退出后进程已死)
    const finalSession = sessionId ?? DEFAULT_SESSIONS[mode] ?? 'chiguo-main'
    const args = [...this.args, '--mode', 'rpc',
      ...buildBaseAgentArgs({ analysisMode: mode === 'analysis', sessionId: finalSession })]
    const proc = spawn(this.bin, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    s.proc = proc
    // spawn 失败(如 bin 不存在)时 proc.pid 为 undefined——不得写入 'undefined' pidfile
    if (proc.pid !== undefined) writeFileSync(this._pidFile(key), String(proc.pid))
    // C1: 所有监听器闭包捕获 proc,回调首行校验归属——restart(超时/dispose)后旧进程
    // SIGTERM 延迟退出时,旧 error/exit/data 回调不得动新会话(置 dead/删 pidfile/reject pending)。
    let readyReject = null
    proc.on('error', (e) => {
      if (s.proc !== proc) return  // 旧进程回调,忽略
      s.dead = true
      this._cleanupPid(key)  // spawn 失败无真实 pid(_cleanupPidIfOurs 的 NaN!==undefined 校验会漏删 'undefined' pidfile)
      const p = s.pending
      s.pending = null
      if (p) p.reject(e)
      if (readyReject) { const r = readyReject; readyReject = null; r(e) }
    })
    // C2: setEncoding('utf8') 让 data 回调收到 string(StringDecoder 内部跨 chunk 拼接
    // 多字节字符)——逐 chunk toString() 会把切在多字节中间的字符解码成 U+FFFD。
    proc.stdout.setEncoding('utf8')
    // A1: stdout 统一走跨 chunk 行缓冲,不假设 chunk 以行边界结束
    proc.stdout.on('data', (d) => {
      if (s.proc !== proc) return  // C1: 旧进程残留 chunk,忽略
      this._onChunk(key, d.toString())
    })
    proc.stderr.on('data', () => {
      if (s.proc !== proc) return  // C1: 旧进程残留输出,忽略
    })
    proc.on('exit', () => {
      if (s.proc !== proc) return  // C1: 旧进程回调,忽略
      s.dead = true
      this._cleanupPidIfOurs(key, proc)
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
        // C1: 若 ready 窗口内发生 restart 且新进程已就位——旧定时器只解除自身 promise
        // 挂起(reject),不得标记新会话 dead 或删新进程 pidfile。
        if (s.proc !== proc) {
          reject(new Error('agent rpc 启动超时(会话已重启)'))
          return
        }
        try { proc.kill('SIGTERM') } catch {}
        s.dead = true
        this._cleanupPidIfOurs(key, proc)
        s.lineHandlers = s.lineHandlers.filter((h) => h !== onLine)
        reject(new Error('agent rpc 启动超时'))
      }, 10_000)
      const onLine = (line) => {
        try {
          const ev = JSON.parse(line)
          if (ev.type === 'response' && ev.id === stateId) {
            clearTimeout(t)
            s.lineHandlers = s.lineHandlers.filter((h) => h !== onLine)
            resolve()
          }
        } catch {}
      }
      s.lineHandlers.push(onLine)
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
        this.restart({ key })  // preflight 失败 → 进程可能处于不确定态,重启让下轮干净启动
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

  /** /new 或 key 轮换后调用:杀指定会话(默认全部)进程,下一轮 prompt 自动重启。
   *  R1: 内部支持直接按 key 重启(lineBuffer 溢出等内部触发的场景)。
   *  #192: async——await 子进程退出(SIGTERM→3s 后 SIGKILL 兜底),调用方可确定「已不再持有会话文件」。
   *  状态(proc=null/dead/lineHandlers/pidfile/pending)在 await 前同步置位,fire-and-forget 调用点语义不变。 */
  async restart({ mode, sessionId, key } = {}) {
    const keys = key ? [key] : mode ? [this._key(mode, sessionId)] : [...this.sessions.keys()]
    await Promise.all(keys.map((k) => this._killSession(k)))
  }

  /** 杀单个会话进程并 await 其退出;绝不抛错(restart 永不 reject)。 */
  async _killSession(key) {
    const s = this.sessions.get(key)
    if (!s) return
    const proc = s.proc
    s.proc = null
    s.dead = true
    s.lineHandlers = []  // M2: 旧握手 handler 作废,防跨轮残留空转
    this._cleanupPid(key)
    const p = s.pending
    s.pending = null
    if (p) p.reject(new Error('agent rpc restart'))
    if (!proc || proc.pid === undefined) return
    await new Promise((resolve) => {
      let settled = false
      // M3: SIGTERM 后延迟复查,未退出补 SIGKILL——防 pi 卡死成孤儿进程
      // 与旧进程残留同写会话文件(pidfile 已删,下次启动也找不到它)。
      const t = setTimeout(() => {
        try { proc.kill('SIGKILL') } catch {}
        done()
      }, 3000)
      t.unref?.()
      const done = () => {
        if (settled) return
        settled = true
        clearTimeout(t)
        resolve()
      }
      proc.once('exit', done)
      proc.once('error', done)
      if (proc.exitCode !== null || proc.signalCode !== null) { done(); return }
      try { proc.kill('SIGTERM') } catch { done() }
    })
  }

  dispose() {
    this.restart()
  }
}

export default AgentRpc
