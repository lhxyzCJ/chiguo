#!/usr/bin/env node
/**
 * wechat-bridge — 微信 (wechatbot fork) ↔ pi-agent 桥接
 *
 * 微信消息 → pi-agent（scripts/pi-run.mjs，chiguo-main 会话）→ 回复发回微信。
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
 *
 * v4（Phase 4 寄主迁移）:
 *  - 回复侧从 openclaw agent 改为 pi-agent：askPi 调 scripts/pi-run.mjs
 *    （--prompt <原文> --analysis-mode），一次完成「情绪分析 JSON + 回复」。
 *  - 分析接线：askPi 返回 analysis 后 → daemon --user-msg <原文> --analysis '<JSON>'
 *    （recv_dedup 升级语义——bridge 已确定性 --user-msg 过，不重复记账）。
 *  - 特殊命令（纪念日/假期）确定性接管：收到消息先 detectSpecialCommand（规则化，
 *    不依赖 pi 输出稳定性），命中 → 直接执行 daemon --anniversary/--break 并回复确认，
 *    不再经 pi（pi 为纯文本调用无工具权限；对应 openclaw standing order 第 4 步）。
 */
import { createServer } from 'node:http'
import { WeChatBot } from '@wechatbot/wechatbot'
import { spawn, execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { pathToFileURL } from 'node:url'
import { detectSpecialCommand, executeSpecialCommand } from './command-detect.mjs'

const execFileP = promisify(execFile)

const DEBOUNCE_MS = 4000
const PI_RUN_SCRIPT = process.env.WECHAT_BRIDGE_PI_RUN
  ?? new URL('../scripts/pi-run.mjs', import.meta.url).pathname
const SEND_PORT = Number(process.env.WECHAT_BRIDGE_SEND_PORT ?? 18790)
const OWNER_ID = process.env.WECHAT_BRIDGE_OWNER ?? 'owner@im.wechat'
const DAEMON_PY = process.env.WECHAT_BRIDGE_DAEMON_PY ?? '/root/chiguo/.venv/bin/python'
const DAEMON_SCRIPT = process.env.WECHAT_BRIDGE_DAEMON ?? '/root/chiguo/chiguo_daemon.py'
// 登录态目录：默认随仓库（wechat-bridge/credentials/，git 跟踪）；可用 WECHAT_BRIDGE_STORAGE 覆盖
const DEFAULT_STORAGE = new URL('./credentials/', import.meta.url).pathname

/** 串行化 pi 调用（同一 pi 会话 chiguo-main 不允许并发 turn，含 chiguo-tick 的周期调用）。 */
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

/** 调用 pi-agent（pi-run.mjs），一次完成「情绪分析 JSON + 回复」。
 * 返回 { text, analysis }；analysis 为解析后的对象或 null。失败抛错。 */
export async function askPi(text) {
  const { stdout } = await execFileP('node', [PI_RUN_SCRIPT, '--prompt', text, '--analysis-mode'], {
    timeout: 180_000,
    maxBuffer: 16 * 1024 * 1024,
  })

  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch {
    throw new Error(`pi-run 输出非 JSON: ${String(stdout).slice(0, 100)}`)
  }
  if (!parsed.ok) {
    throw new Error(parsed.error ?? 'pi-run 返回 ok=false 且无 error')
  }
  return { text: parsed.text, analysis: parsed.analysis ?? null }
}

/** 确定性记录主人消息到迟菓 daemon（无分析；随后的 askPi 分析经 upgradeAnalysis 升级，daemon 去重）。失败不阻塞回复流。 */
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

/** 分析升级：askPi 已产出情绪分析 JSON → daemon --user-msg --analysis。
 * recv_dedup 升级语义：同一原文（600s 窗口内）只补分析微调，不重复记账。失败不阻塞回复流。 */
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

/** 单条微信消息处理链路（onMessage 委托；导出供测试）：
 * 1) recordUserMsg 确定性回传 daemon（无分析）
 * 2) detectSpecialCommand 命中特殊命令 → executeSpecialCommand 直接执行 daemon 并回复，不经 pi
 * 3) 否则 askPi（pi-run --analysis-mode 一次完成分析+回复）→ upgradeAnalysis 升级 → 回复
 * bot 需提供 reply(msg, text)/sendTyping(userId)；queue 提供 run(task)。 */
export async function handleMessage(text, msg, bot, queue) {
  if (!text?.trim()) return null

  await recordUserMsg(text)  // 确定性回传 daemon（先于 askPi 分析；analysis 稍后经 upgradeAnalysis 升级）

  // 特殊命令（纪念日/假期）确定性接管：命中则直接执行 daemon，不经 pi（Phase 4 Task 14）
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

  try {
    await bot.sendTyping(msg.userId).catch(() => {})
  } catch {}

  await queue
    .run(async () => {
      try {
        const { text: reply, analysis } = await askPi(text)
        await upgradeAnalysis(text, analysis)  // recv_dedup 升级语义，不重复记账
        console.log(`[out] ${reply.slice(0, 80)}`)
        await bot.reply(msg, reply)
      } catch (err) {
        const reason = err instanceof Error ? err.message : String(err)
        console.error('[pi error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason.slice(0, 100)}`).catch(() => {})
      }
    })
    .catch((err) => console.error('[queue error]', err))
  return 'pi'
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
    await handleMessage(text, msg, bot, queue)
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
