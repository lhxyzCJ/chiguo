#!/usr/bin/env node
/**
 * wechat-bridge — 微信 (wechatbot fork) ↔ OpenClaw agent 桥接
 *
 * 微信消息 → openclaw agent (main session) → 回复发回微信。
 * 使用 fork 的 inboundDebounce 合并连发文本（windowMs 4000）。
 *
 * v2 新增（迟菓主动链路）:
 *  - 主动发送端点: POST http://127.0.0.1:18790/send {"to","text"} → bot.send()
 *    （agent 生成消息后 curl 调用；仅允许发给 OWNER_ID）
 *  - 回复确定性回传: 收到主人消息先跑 chiguo_daemon.py --user-msg（无分析），
 *    standing order 随后由 agent 补 --analysis（daemon recv_dedup 升级语义，不重复记账）。
 *
 * v3 可移植化（随 chiguo 仓库部署）:
 *  - storageDir 默认 = 本文件同目录 credentials/（git 跟踪 → 登录态随仓库走；
 *    失效时 SDK 打印二维码重新扫码，即"尝试保留"）。绝不写入 wechatbot 仓库。
 *  - 所有路径/端口/主人 ID 可用 WECHAT_BRIDGE_* 环境变量覆盖（scripts/wechat-bridge.sh 生成 .env）。
 */
import { createServer } from 'node:http'
import { WeChatBot } from '@wechatbot/wechatbot'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

const execFileP = promisify(execFile)

const DEBOUNCE_MS = 4000
const SESSION_KEY = process.env.WECHAT_BRIDGE_SESSION_KEY ?? 'agent:main:main'
const AGENT_BIN = process.env.WECHAT_BRIDGE_AGENT ?? 'openclaw'
const SEND_PORT = Number(process.env.WECHAT_BRIDGE_SEND_PORT ?? 18790)
const OWNER_ID = process.env.WECHAT_BRIDGE_OWNER ?? 'owner@im.wechat'
const DAEMON_PY = process.env.WECHAT_BRIDGE_DAEMON_PY ?? '/root/chiguo/.venv/bin/python'
const DAEMON_SCRIPT = process.env.WECHAT_BRIDGE_DAEMON ?? '/root/chiguo/chiguo_daemon.py'
// 登录态目录：默认随仓库（wechat-bridge/credentials/，git 跟踪）；可用 WECHAT_BRIDGE_STORAGE 覆盖
const DEFAULT_STORAGE = new URL('./credentials/', import.meta.url).pathname

/** 串行化 agent 调用（main session 不允许并发 turn）。 */
class TurnQueue {
  constructor() {
    this.tail = Promise.resolve()
  }

  run(task) {
    const next = this.tail.then(task, task)
    this.tail = next.catch(() => {})
    return next
  }
}

/** 调用 openclaw agent，返回回复文本。失败抛错。 */
async function askOpenClaw(text) {
  const { stdout } = await execFileP(AGENT_BIN, [
    'agent',
    '--session-key', SESSION_KEY,
    '-m', text,
    '--json',
  ], { timeout: 180_000, maxBuffer: 10 * 1024 * 1024 })

  const parsed = JSON.parse(stdout)
  if (parsed.status !== 'ok') {
    throw new Error(`agent status=${parsed.status} summary=${parsed.summary ?? ''}`)
  }
  const payloads = parsed.result?.payloads ?? []
  const reply = payloads
    .map((p) => p.text)
    .filter((t) => typeof t === 'string' && t.length > 0)
    .join('\n')
  if (!reply) throw new Error('agent returned empty reply')
  return reply
}

/** 确定性记录主人消息到迟菓 daemon（无分析；standing order 稍后补分析，daemon 去重升级）。失败不阻塞回复流。 */
async function recordUserMsg(text) {
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

/** 主动发送端点：POST /send {"to","text"} → bot.send()。仅 127.0.0.1。 */
function startSendServer(bot) {
  const server = createServer((req, res) => {
    if (req.method !== 'POST' || req.url !== '/send') {
      res.writeHead(405, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error: 'only POST /send' }))
      return
    }
    let body = ''
    req.on('data', (c) => {
      body += c
      if (body.length > 1_000_000) {
        res.writeHead(413, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ ok: false, error: 'payload too large' }))
        req.destroy()
      }
    })
    req.on('error', () => {})  // destroy 后连接重置，避免未处理错误事件
    req.on('end', async () => {
      try {
        const { to, text } = JSON.parse(body || '{}')
        if (typeof to !== 'string' || !to.trim()) throw new Error('to 必填')
        if (typeof text !== 'string' || !text.trim()) throw new Error('text 必填')
        if (to !== OWNER_ID) {
          res.writeHead(403, { 'Content-Type': 'application/json' })
          res.end(JSON.stringify({ ok: false, error: 'forbidden recipient' }))
          return
        }
        await bot.send(to, text)
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
  server.listen(SEND_PORT, '127.0.0.1', () => {
    console.log(`send server: http://127.0.0.1:${SEND_PORT}/send`)
  })
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

    await recordUserMsg(text)  // 确定性回传 daemon（先于 agent 的 standing order 分析）

    try {
      await bot.sendTyping(msg.userId).catch(() => {})
    } catch {}

    queue
      .run(async () => {
        try {
          const reply = await askOpenClaw(text)
          console.log(`[out] ${reply.slice(0, 80)}`)
          await bot.reply(msg, reply)
        } catch (err) {
          const reason = err instanceof Error ? err.message : String(err)
          console.error('[agent error]', reason)
          await bot.reply(msg, `⚠️ 处理失败：${reason.slice(0, 100)}`).catch(() => {})
        }
      })
      .catch((err) => console.error('[queue error]', err))
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

main().catch((err) => {
  console.error('启动失败:', err instanceof Error ? err.message : String(err))
  process.exit(1)
})
