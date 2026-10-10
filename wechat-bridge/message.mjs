/** wechat-bridge/message.mjs — 消息管线（handleMessage + askChat + 白名单门）。
 * 路由顺序: 白名单门(F-SEC-03,最顶部) → OWNER_ID 门(C1) → 斜杠命令 → askAgent。
 * 非 owner(仅白名单内) = 仅 askAgent 回复 + 失败回通用文案(不回内部诊断,安全补钉);
 * 非白名单(含缺省仅 owner) = 固定拒答文案 + 零 LLM 调用(返回 'rejected')。 */
import { detectSlashCommand, executeSlashCommand } from './command-detect.mjs'
import { currentOwnerId, REJECT_TEXT, AGENT_RPC_ENABLED, BRIDGE_DIR, isAllowedContact } from './env.mjs'
import { sanitizeError } from './util.mjs'
import { askAgent } from './agent.mjs'

/** 聊天链回复:queue 串行 + askAgent;失败回通用文案(非本人/chat 放行共用,不回内部诊断)。 */
async function askChat(text, msg, bot, queue, askAgentFn) {
  await queue.run(async () => {
    try {
      const { text: reply } = await askAgentFn(text)
      await bot.reply(msg, reply).catch(() => {})
    } catch {
      await bot.reply(msg, '⚠️ 处理失败').catch(() => {})   // 不回内部诊断(安全补钉)
    }
  })
}

/** 单条微信消息处理链路（onMessage 委托）：
 * bot 需提供 reply(msg, text)/sendTyping(userId)；queue 提供 run(task)。 */
export async function handleMessage(text, msg, bot, queue, deps = {}) {
  if (!text?.trim()) return null
  // owner 取实时值（登录后落盘的 credentials.json 优先）：启动快照在新登录后过期，
  // 用快照会把用户判成陌生人（F-SEC-03 拒答）。测试可经 deps.ownerId 注入。
  const ownerId = deps.ownerId ?? currentOwnerId()
  const isOwner = msg.userId === ownerId
  const askAgentFn = deps.askAgent ?? askAgent

  // ── 白名单门:非白名单(含缺省=仅 owner)→ 固定拒答文案,不调 askAgent(零 LLM 成本) ──
  if (!isAllowedContact(msg.userId, deps.whitelist, ownerId)) {
    await bot.reply(msg, REJECT_TEXT).catch(() => console.warn('[whitelist reject] 拒答回复发送失败'))
    return 'rejected'
  }

  // ── C1 门:非 owner(仅白名单内)不进斜杠命令路径,仅 askAgent 回复 ──
  if (!isOwner) {
    await askChat(text, msg, bot, queue, askAgentFn)
    return 'agent'
  }

  // 微信端斜杠命令（白名单制）：全部 / 开头消息确定性接管，不经 pi
  const slash = detectSlashCommand(text)
  if (slash) {
    await queue.run(async () => {
      try {
        // /new 先重启常驻 agent(await 子进程退出、释放旧会话文件),再备份会话文件——
        // 否则备份的是旧进程仍持有的会话文件,时序错乱。
        if (slash.action === 'new_session' && AGENT_RPC_ENABLED && globalThis.__agentRpc) {
          await globalThis.__agentRpc.restart()
          console.log('[slash] agent-rpc 已重启(新会话)')
        }
        const r = await executeSlashCommand(slash, BRIDGE_DIR)
        console.log(`[slash] ${slash.action} → ok=${r.ok}`)
        await bot.reply(msg, r.reply)
      } catch (err) {
        const raw = err instanceof Error ? err.message : String(err)
        const reason = sanitizeError(raw, text)
        console.error('[slash error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason}`).catch(() => {})
      }
    })
    return 'slash'
  }

  try {
    await bot.sendTyping(msg.userId).catch(() => {})
  } catch {}

  await queue
    .run(async () => {
      try {
        const { text: reply } = await askAgentFn(text)
        console.log(`[out] ${(reply ?? '').length} chars`)
        await bot.reply(msg, reply).catch((e) => console.error('[reply error]', e))
      } catch (err) {
        const raw = err instanceof Error ? err.message : String(err)
        const reason = sanitizeError(raw, text)
        console.error('[agent error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason}`).catch(() => {})
      }
    })
    .catch((err) => console.error('[queue error]', err))
  return 'agent'
}
