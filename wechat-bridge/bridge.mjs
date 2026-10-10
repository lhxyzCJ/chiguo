#!/usr/bin/env node
/**
 * wechat-bridge — 微信 (wechatbot fork) ↔ pi-agent 桥接
 *
 * 微信消息 → pi-agent（scripts/agent-run.mjs，chiguo-main 会话）→ 回复发回微信。
 * 使用 fork 的 inboundDebounce 合并连发文本（windowMs 4000）。
 *
 * 主动发送端点: POST http://127.0.0.1:18790/send {"to","text"} → bot.send()（仅允许发给 OWNER_ID）。
 * 主会话每日轮换: session-rotate.mjs armSessionRotation（整点检查，空闲超阈值才轮换）。
 *
 * v3 可移植化（随 chiguo 仓库部署）:
 *  - storageDir 默认 = 本文件同目录 credentials/（仅本地保留，不进 git（隐私）；
 *    失效时 SDK 打印二维码重新扫码，即"尝试保留"）。绝不写入 wechatbot 仓库。
 *  - 所有路径/端口/用户 ID 可用 WECHAT_BRIDGE_* 环境变量覆盖（scripts/wechat-bridge.sh 生成 .env）。
 */
import { mkdirSync, chmodSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { WeChatBot } from '@wechatbot/wechatbot'
import { defaultRotatePaths, writeActivity, armSessionRotation } from './session-rotate.mjs'
import { BRIDGE_TOKEN, AGENT_RUN_SCRIPT, DEFAULT_STORAGE, DEBOUNCE_MS } from './env.mjs'
import { TurnQueue } from './queue.mjs'
import { checkAgentRunScript } from './agent.mjs'
import { startSendServer } from './send.mjs'
import { handleMessage } from './message.mjs'

// 隐私收紧：bridge 常驻 RPC（agent-rpc 直 spawn pi）与 agent-run 回退产生的会话文件
// 都继承本 umask → 会话 JSONL 0600、目录 0700（默认 umask 下为 0644）。
process.umask(0o077)

async function main() {
  // #191: 未设置共享 token 时 /send 零鉴权(同机任意进程可冒充 owner)→ 拒绝启动。
  // wechat-bridge.sh 已自动生成并注入 token,故此处仅命中「直接 node bridge.mjs 绕过启动脚本」的场景。
  if (!BRIDGE_TOKEN) {
    console.error(
      '[FATAL] WECHAT_BRIDGE_TOKEN 未设置:HTTP 端点(/send)零鉴权,拒绝启动。\n' +
      '       请通过 wechat-bridge.sh 启动,或手动生成 token 写入 .env:\n' +
      '      echo "WECHAT_BRIDGE_TOKEN=$(openssl rand -hex 16)" >> .env\n')
    process.exit(1)
  }
  // U8c: AGENT_RUN_SCRIPT 启动时校验——缺失/脚本不存在 → 明确报错退出(替代 ask 期通用失败文案,便于诊断)。
  // 默认已按仓库内 scripts/agent-run.mjs 落地;此处兜底命中「env 显式指向错误/文件缺失」场景。
  const agentRunErr = checkAgentRunScript(AGENT_RUN_SCRIPT)
  if (agentRunErr) {
    console.error(`[FATAL] ${agentRunErr}\n       请通过 wechat-bridge.sh 启动(其自动注入 WECHAT_BRIDGE_AGENT_RUN=scripts/agent-run.mjs),或确认 agent 调用层脚本存在。`)
    process.exit(1)
  }
  // 登录态目录含微信登录凭证 → 强制 0o700,防同机其他用户读取会话/凭证文件(umask 宽松时兜底)
  const storageDir = process.env.WECHAT_BRIDGE_STORAGE ?? DEFAULT_STORAGE
  mkdirSync(storageDir, { recursive: true, mode: 0o700 })
  chmodSync(storageDir, 0o700)
  // 注意：SDK 只认 login()/run() 参数里的 callbacks，构造器 loginCallbacks 字段声明了但从未被读取
  //（client.ts:41），回调必须在 login() 时显式传入，否则 SDK 自己打 "Scan this QR..." 日志，脚本抓不到码。
  const loginCallbacks = {
    onQrUrl: (url) => {
      console.log('\n=== 微信扫码登录 ===')
      // 二维码链接含登录凭证,默认打印;WECHAT_BRIDGE_QR_LOG=0 可关闭(日志分享场景防泄漏)
      if (process.env.WECHAT_BRIDGE_QR_LOG === '0') console.log('[QR 隐藏] 设 WECHAT_BRIDGE_QR_LOG!=0 可打印二维码链接')
      else console.log(url)
      console.log('====================\n')
    },
    onScanned: () => console.log('已扫码，等待确认…'),
    onExpired: () => console.log('二维码已过期，刷新中…'),
  }
  const bot = new WeChatBot({
    storage: 'file',
    storageDir,
    logLevel: 'info',
    inboundDebounce: {
      windowMs: DEBOUNCE_MS,
      joinSeparator: '\n',
    },
    loginCallbacks,
  })

  const queue = new TurnQueue()
  const activityPath = process.env.WECHAT_BRIDGE_ACTIVITY_FILE ?? defaultRotatePaths().activityFile

  bot.onMessage(async (msg) => {
    const text = msg.text
    if (!text?.trim()) return
    console.log(`[in] ${msg.userId}: ${text.length} chars`)  // 脱敏：不落正文（仅长度）
    try { writeActivity(activityPath) } catch {}   // 用户主动消息 = 会话活动（best-effort，写失败不阻塞消息链）
    await handleMessage(text, msg, bot, queue)
  })

  bot.on('error', (err) => {
    console.error('[bot error]', err instanceof Error ? err.message : String(err))
  })
  bot.on('session:expired', () => console.warn('[bot] 会话过期，尝试重登…'))

  await bot.login({ callbacks: loginCallbacks })
  // 该 fork 的 bot.start() 长轮询挂起不返回 → 主动发送端点必须先于 start 就绪
  startSendServer(bot)
  armSessionRotation(queue)
  await bot.start()
  console.log('wechat-bridge 运行中（Ctrl+C 停止）')
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => {
    console.error('启动失败:', err instanceof Error ? err.message : String(err))
    process.exit(1)
  })
}
