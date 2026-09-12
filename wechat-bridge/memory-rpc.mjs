#!/usr/bin/env node
/**
 * memory-rpc.mjs — 常驻只读记忆边车(memory/server.py)客户端。
 *
 * 动机(Issue #450,2C):回复链每条消息 spawn 全新 python 跑 --memory-search,
 * mem0 懒加载 + qdrant 嵌入式开库冷启动 4~13s。边车抱住 warm backend,同进程
 * 重复检索 ~0.5s。
 *
 * 骨架照抄 agent-rpc.mjs:spawn + stdio NDJSON + pidfile + ping preflight +
 * 失败 restart + 单 pending(bridge TurnQueue 已串行)。差异:单会话、无业务
 * 模板、响应即整行 JSON(无流式事件)。
 *
 * 安全网:启动/调用任何失败 → 抛错,调用方(agent.mjs)回退旧 spawn 路径;
 * 边车崩溃 → 下一轮 query 自动重启;bridge 退出 → 边车随 stdin EOF 退出。
 */
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync, readFileSync, existsSync, unlinkSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import { homeDir } from './home-dir.mjs'
import { DAEMON_PY, REPO_ROOT } from './env.mjs'

const PID_DIR = join(homeDir(), '.pi', 'agent')
const PID_PREFIX = 'memory-rpc-'
const SERVER_SCRIPT = join(REPO_ROOT, 'memory', 'server.py')
const MAX_LINE_BUFFER = 1024 * 1024
const READY_TIMEOUT_MS = 10_000
const QUERY_TIMEOUT_MS = 30_000

export class MemoryRpc {
  constructor({ bin = DAEMON_PY, args = [SERVER_SCRIPT] } = {}) {
    this.bin = bin
    this.args = args
    this.proc = null
    this.dead = true
    this.buffer = ''
    this.pending = null  // {id, resolve, reject} 单飞行请求
    this.lineHandlers = []
    this.seq = 0
    mkdirSync(PID_DIR, { recursive: true, mode: 0o700 })
    this._killStale()
  }

  _pidFile() {
    return join(PID_DIR, `${PID_PREFIX}chiguo.pid`)
  }

  /** 防孤儿:杀掉本实例之外的旧边车。pid 复用防护——确认 cmdline 含 memory/server.py 才杀。 */
  _killStale() {
    try {
      const files = readdirSync(PID_DIR).filter((f) => f.startsWith(PID_PREFIX))
      for (const f of files) {
        try {
          const old = Number(readFileSync(join(PID_DIR, f), 'utf8').trim())
          if (old > 0 && old !== process.pid && this._isOurServer(old)) {
            try { process.kill(old, 'SIGTERM') } catch {}
          }
          unlinkSync(join(PID_DIR, f))
        } catch {}
      }
    } catch {}
  }

  _isOurServer(pid) {
    try {
      process.kill(pid, 0)
      if (process.platform !== 'linux') return true
      // 精确匹配：独立 token 'memory/server.py'（防 'not-memory/server.py' 类后缀误杀；
      // 对齐 agent-rpc.mjs 独立 token 匹配思想；解释器不限，uv/python 均可）。
      const argv = readFileSync(`/proc/${pid}/cmdline`, 'utf8').split('\0')
      return argv.some((a) => a === 'memory/server.py' || a.endsWith('/memory/server.py'))
    } catch {
      return false
    }
  }

  _cleanupPid() {
    try {
      const p = this._pidFile()
      if (existsSync(p)) unlinkSync(p)
    } catch {}
  }

  _cleanupPidIfOurs(proc) {
    try {
      const p = this._pidFile()
      if (existsSync(p) && Number(readFileSync(p, 'utf8').trim()) === proc.pid) unlinkSync(p)
    } catch {}
  }

  _onChunk(chunk) {
    this.buffer += chunk
    let nl
    while ((nl = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, nl)
      this.buffer = this.buffer.slice(nl + 1)
      this._handleLine(line)
    }
    if (this.buffer.length > MAX_LINE_BUFFER) {
      console.error(`[memory-rpc] 行缓冲超限,丢弃残段并重启`)
      this.buffer = ''
      this.restart()
    }
  }

  _handleLine(line) {
    const trimmed = line.trim()
    if (!trimmed) return
    // 先交握手 handler(ready 判定),再交 pending 匹配
    for (const h of this.lineHandlers) {
      try { h(trimmed) } catch {}
    }
    if (!this.pending) return
    try {
      const resp = JSON.parse(trimmed)
      if (resp.id === this.pending.id) {
        const p = this.pending
        this.pending = null
        p.resolve(resp)
      }
    } catch {}
  }

  async ensureStarted() {
    if (this.proc && !this.dead) return
    this.dead = false
    this.buffer = ''
    this.lineHandlers = []
    const proc = spawn(this.bin, this.args, { stdio: ['pipe', 'pipe', 'pipe'] })
    this.proc = proc
    if (proc.pid !== undefined) writeFileSync(this._pidFile(), String(proc.pid))
    let readyReject = null
    proc.on('error', (e) => {
      if (this.proc !== proc) return
      this.dead = true
      this._cleanupPid()
      const p = this.pending
      this.pending = null
      if (p) p.reject(e)
      if (readyReject) { const r = readyReject; readyReject = null; r(e) }
    })
    proc.stdout.setEncoding('utf8')
    proc.stdout.on('data', (d) => {
      if (this.proc !== proc) return
      this._onChunk(d.toString())
    })
    proc.stderr.on('data', () => {
      if (this.proc !== proc) return
    })
    proc.on('exit', () => {
      if (this.proc !== proc) return
      this.dead = true
      this._cleanupPidIfOurs(proc)
      const p = this.pending
      this.pending = null
      if (p) p.reject(new Error('memory 边车进程退出'))
    })
    // 就绪握手:ping 命令收到同 id 响应即确认
    await new Promise((resolve, reject) => {
      readyReject = reject
      const pingId = `ready-${Date.now()}`
      const t = setTimeout(() => {
        if (this.proc !== proc) {
          reject(new Error('memory 边车启动超时(已重启)'))
          return
        }
        try { proc.kill('SIGTERM') } catch {}
        this.dead = true
        this._cleanupPidIfOurs(proc)
        this.lineHandlers = this.lineHandlers.filter((h) => h !== onLine)
        reject(new Error('memory 边车启动超时'))
      }, READY_TIMEOUT_MS)
      const onLine = (line) => {
        try {
          const resp = JSON.parse(line)
          if (resp.id === pingId && resp.ok) {
            clearTimeout(t)
            this.lineHandlers = this.lineHandlers.filter((h) => h !== onLine)
            resolve()
          }
        } catch {}
      }
      this.lineHandlers.push(onLine)
      try {
        proc.stdin.write(`${JSON.stringify({ id: pingId, cmd: 'ping' })}\n`)
      } catch (e) {
        clearTimeout(t)
        reject(e)
      }
    })
  }

  /** 发查询并等整行响应。失败抛错（调用方回退 spawn）。 */
  async query(cmd, params = {}) {
    await this.ensureStarted()
    if (this.pending) throw new Error('memory 边车已有进行中查询(不应发生:bridge TurnQueue 串行)')
    const id = `q-${Date.now()}-${++this.seq}`
    this.pending = { id, resolve: null, reject: null }
    const done = new Promise((res, rej) => {
      this.pending.resolve = res
      this.pending.reject = rej
    })
    try {
      this.proc.stdin.write(`${JSON.stringify({ id, cmd, ...params })}\n`)
    } catch (e) {
      this.pending = null
      throw e
    }
    let timedOut = false
    const timer = setTimeout(() => {
      timedOut = true
      const p = this.pending
      this.pending = null
      if (p) p.reject(new Error(`memory 边车查询超时(${QUERY_TIMEOUT_MS}ms)`))
    }, QUERY_TIMEOUT_MS)
    try {
      const resp = await done
      if (!resp.ok) throw new Error(resp.reason ?? 'memory 边车返回 ok=false')
      return resp
    } finally {
      clearTimeout(timer)
      if (timedOut) this.restart()
    }
  }

  async restart() {
    const proc = this.proc
    this.proc = null
    this.dead = true
    this.lineHandlers = []
    this._cleanupPid()
    const p = this.pending
    this.pending = null
    if (p) p.reject(new Error('memory 边车 restart'))
    if (!proc || proc.pid === undefined) return
    await new Promise((resolve) => {
      let settled = false
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

export default MemoryRpc
