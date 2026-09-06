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

// Issue #380 barrel：本文件仅保留 main 启动装配 + 全量 re-export（入口路径不变，
// service.sh / wechat-bridge.sh / test_service.sh / 子进程直起均不受影响）。
// 各域实现：env.mjs（env 快照）/ util.mjs（withTimeout/sanitizeError）/
// queue.mjs（TurnQueue）/ agent.mjs（askAgent 全家 + /agent/prompt）/
// send.mjs（HTTP 端点 + 鉴权中间件）/ schedule.mjs（澄清 + 命令链路 + 轮换）/
// message.mjs（消息管线）。
import { mkdirSync, chmodSync } from 'node:fs'
import { pathToFileURL } from 'node:url'
import { WeChatBot } from '@wechatbot/wechatbot'
import { defaultRotatePaths, writeActivity } from './session-rotate.mjs'
import { BRIDGE_TOKEN, AGENT_RUN_SCRIPT, DEFAULT_STORAGE, DEBOUNCE_MS, REPO_ROOT } from './env.mjs'
import { TurnQueue } from './queue.mjs'
import { checkAgentRunScript } from './agent.mjs'
import { startSendServer } from './send.mjs'
import { makeScheduleDeps, armSessionRotation } from './schedule.mjs'
import { handleMessage } from './message.mjs'

// 全量 re-export：测试与外部调用方从 bridge.mjs 取的全部符号（isLocalHost/isLocalOrigin/
// askAgent 全家/TurnQueue/handleMessage/sendMessage/澄清存取/checkAgentRunScript 等）
// 经此透出，import 源零改动。export * 无命名冲突（各模块导出集合不交）。
export * from './env.mjs'
export * from './util.mjs'
export * from './queue.mjs'
export * from './agent.mjs'
export * from './health.mjs'
export * from './send.mjs'
export * from './schedule.mjs'
export * from './message.mjs'

async function main() {
  // #191: 未设置共享 token 时 /send 与 /agent/prompt 零鉴权(同机任意进程可冒充 owner)→ 拒绝启动。
  // wechat-bridge.sh 已自动生成并注入 token,故此处仅命中「直接 node bridge.mjs 绕过启动脚本」的场景。
  if (!BRIDGE_TOKEN) {
    console.error(
      '[FATAL] WECHAT_BRIDGE_TOKEN 未设置:HTTP 端点(/send 与 /agent/prompt)零鉴权,拒绝启动。\n' +
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
  const bot = new WeChatBot({
    storage: 'file',
    storageDir,
    logLevel: 'info',
    inboundDebounce: {
      windowMs: DEBOUNCE_MS,
      joinSeparator: '\n',
    },
    loginCallbacks: {
      onQrUrl: (url) => {
        console.log('\n=== 微信扫码登录 ===')
        // 二维码链接含登录凭证,默认打印;WECHAT_BRIDGE_QR_LOG=0 可关闭(日志分享/CI 场景防泄漏)
        if (process.env.WECHAT_BRIDGE_QR_LOG === '0') console.log('[QR 隐藏] 设 WECHAT_BRIDGE_QR_LOG!=0 可打印二维码链接')
        else console.log(url)
        console.log('====================\n')
      },
      onScanned: () => console.log('已扫码，等待确认…'),
      onExpired: () => console.log('二维码已过期，刷新中…'),
    },
  })

  const queue = new TurnQueue()
  const activityPath = process.env.WECHAT_BRIDGE_ACTIVITY_FILE ?? defaultRotatePaths().activityFile

  bot.onMessage(async (msg) => {
    const text = msg.text
    if (!text?.trim()) return
    console.log(`[in] ${msg.userId}: ${text.length} chars`)  // 脱敏：不落正文（仅长度）
    try { writeActivity(activityPath) } catch {}   // 用户主动消息 = 会话活动（best-effort，写失败不阻塞消息链）
    await handleMessage(text, msg, bot, queue, makeScheduleDeps(REPO_ROOT))
  })

  bot.on('error', (err) => {
    console.error('[bot error]', err instanceof Error ? err.message : String(err))
  })
  bot.on('session:expired', () => console.warn('[bot] 会话过期，尝试重登…'))

  await bot.login()
  // 该 fork 的 bot.start() 长轮询挂起不返回 → 主动发送端点必须先于 start 就绪
  startSendServer(bot, queue)
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
