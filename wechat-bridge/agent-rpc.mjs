#!/usr/bin/env node
/**
 * pi-rpc.mjs — 常驻 pi 进程(RPC 模式)客户端,供 bridge 回复链复用
 *
 * 架构动机(对比 OpenClaw 常驻 gateway):每次消息 spawn 新 pi 进程会重复
 * 扩展初始化(LanceDB 打开/FTS/健康检查)。RPC 常驻让 pi + 记忆扩展只初始化一次,
 * 每消息走 stdio JSON-RPC prompt,省去 per-message 进程/初始化开销。
 *
 * 协议(pi dist/modes/rpc):stdin 发 {type,id,...} 命令;stdout 流式输出
 * AgentSessionEvent(含 message_end 的 message.usage)+ {type:'response'} 响应。
 * prompt 命令的 response 在 preflight 即回;回合完成以 agent_settled 事件为准。
 *
 * 安全网:启动/调用任何失败 → 抛错,调用方(bridge)回退旧 spawn 路径;
 * 进程崩溃 → 下一轮 prompt 自动重启;bridge 退出 → 子进程随 stdin EOF 退出,
 * 另用 pidfile 防孤儿(新实例启动时杀掉旧 pid)。
 */
import { spawn } from 'node:child_process'
import { writeFileSync, readFileSync, existsSync, unlinkSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { homedir } from 'node:os'
import {
  buildBasePiArgs, buildAnalysisPrompt, parseNdjson, extractAnalysis, parseUsage, appendTelemetry, PI_TIMEOUT,
} from '../scripts/pi-run.mjs'

const PID_FILE = join(homedir(), '.pi', 'agent', 'pi-rpc.pid')

export class PiRpc {
  constructor({ bin = process.env.PI_BIN ?? 'pi' } = {}) {
    this.bin = bin
    this.proc = null
    this.buffer = []
    this.pending = null
    this.dead = true
    this._killStale()
  }

  _killStale() {
    try {
      if (existsSync(PID_FILE)) {
        const old = Number(readFileSync(PID_FILE, 'utf8').trim())
        if (old > 0 && old !== process.pid) {
          try { process.kill(old, 'SIGTERM') } catch {}
        }
        unlinkSync(PID_FILE)
      }
    } catch {}
  }

  _cleanupPid() {
    try { if (existsSync(PID_FILE)) unlinkSync(PID_FILE) } catch {}
  }

  async ensureStarted() {
    if (this.proc && !this.dead) return
    this.dead = false
    this.buffer = []
    const args = ['--mode', 'rpc', ...buildBasePiArgs({ analysisMode: true })]
    const proc = spawn(this.bin, args, { stdio: ['pipe', 'pipe', 'pipe'] })
    this.proc = proc
    writeFileSync(PID_FILE, String(proc.pid))
    // #84: spawn 失败(bin 不存在等)或运行期异常 → 立即标记死亡并 reject ready/pending,不等 10s 超时
    let readyReject = null
    proc.on('error', (e) => {
      this.dead = true
      this._cleanupPid()
      const p = this.pending
      this.pending = null
      if (p) p.reject(e)
      if (readyReject) { const r = readyReject; readyReject = null; r(e) }
    })
    proc.stdout.on('data', (d) => {
      const lines = d.toString().split('\n')
      for (const line of lines) {
        if (!line.trim()) continue
        this.buffer.push(line)
        this._maybeSettle(line)
      }
    })
    proc.stderr.on('data', () => {})
    proc.on('exit', () => {
      this.dead = true
      this._cleanupPid()
      const p = this.pending
      this.pending = null
      if (p) p.reject(new Error('pi rpc 进程退出'))
    })
    // 等待 RPC 就绪:发 get_state 命令,收到 response 即确认(stdin/stdout 通路 + 会话已绑定)
    await new Promise((resolve, reject) => {
      readyReject = reject
      const stateId = `ready-${Date.now()}`
      const t = setTimeout(() => reject(new Error('pi rpc 启动超时')), 10_000)
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

  _maybeSettle(line) {
    if (!this.pending) return
    try {
      const ev = JSON.parse(line)
      if (ev.type === 'response' && ev.command === 'prompt' && ev.success === false) {
        const p = this.pending
        this.pending = null
        p.reject(new Error(ev.error ?? 'prompt preflight 失败'))
        return
      }
      if (ev.type === 'agent_settled' || ev.type === 'message_end') {
        const p = this.pending
        if (p && ev.type === 'agent_settled') {
          this.pending = null
          p.resolve()
        }
      }
    } catch {}
  }

  /** 发 prompt 并等待回合完成,返回 bridge 形状 {text, analysis}。失败抛错。 */
  async prompt(message) {
    const t0 = Date.now()
    await this.ensureStarted()
    if (this.pending) throw new Error('RPC 已有进行中的 prompt(不应发生:bridge TurnQueue 串行)')
    const bufStart = this.buffer.length
    this.pending = { resolve: null, reject: null }
    const done = new Promise((res, rej) => {
      this.pending.resolve = res
      this.pending.reject = rej
    })
    this.proc.stdin.write(`${JSON.stringify({ type: 'prompt', message: buildAnalysisPrompt(message) })}\n`)
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      const p = this.pending
      this.pending = null
      if (p) p.reject(new Error(`pi rpc 超时(${PI_TIMEOUT}ms)`))
    }, PI_TIMEOUT)
    try {
      await done
      const slice = this.buffer.slice(bufStart)
      const text = parseNdjson(slice.join('\n'))
      const usage = parseUsage(slice.join('\n'))
      appendTelemetry({
        ts: new Date().toISOString(), mode: 'analysis',
        dur_ms: Date.now() - t0, ok: !!text, text_len: text?.length ?? 0, usage: usage ?? null,
      })
      if (!text) throw new Error('RPC 空回复')
      const { analysis, reply } = extractAnalysis(text)
      return { text: reply, analysis }
    } finally {
      clearTimeout(timer)
      this.buffer = this.buffer.slice(bufStart)  // #84: 本轮数据消费后裁剪,防 buffer 无界增长
      if (timedOut) this.restart()
    }
  }

  /** /new 后调用:杀掉进程,下一轮 prompt 自动重启(重载最新 chiguo-main)。 */
  restart() {
    try { this.proc?.kill('SIGTERM') } catch {}
    this.proc = null
    this.dead = true
    const p = this.pending
    this.pending = null
    if (p) p.reject(new Error('pi rpc restart'))
  }

  dispose() {
    this.restart()
  }
}

export default PiRpc
