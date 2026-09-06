/** wechat-bridge/message.mjs — 消息管线（handleMessage + askChat + 白名单门）。
 * 从 bridge.mjs 纯搬运；最多依赖的一层（env + command-detect + agent + schedule + health），
 * 不被任何模块 import（防循环），仅 barrel 组装 main 时使用。 */
import { randomUUID } from 'node:crypto'
import { detectSpecialCommand, executeSpecialCommand, detectScheduleIntent, detectSlashCommand, executeSlashCommand } from './command-detect.mjs'
import { OWNER_ID, REJECT_TEXT, AGENT_RPC_ENABLED, BRIDGE_DIR, DAEMON_PY, DAEMON_SCRIPT, REPO_ROOT, isAllowedContact } from './env.mjs'
import { sanitizeError } from './util.mjs'
import { spawn, askAgent, askAgentWithAttention, getAttention, getMemories, runWithRecall, recordUserMsg, upgradeAnalysis } from './agent.mjs'
import { recordAgentHealth } from './health.mjs'
import { readClarify, writeClarify, clearClarify, exitWordMatch, defaultExtractAgent, defaultVerifyAgent, defaultRunDaemon, handleScheduleCommand } from './schedule.mjs'

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
 * 路由顺序:白名单门(F-SEC-03,最顶部) → OWNER_ID 门(C1) → recordUserMsg(仅本人) → 澄清检查
 * → detectSpecialCommand → detectScheduleIntent → askAgent;命令消息经 --user-msg 无分析(recordUserMsg
 * 已先行,dedup 450s 继承);
 * 非 owner(仅白名单内) = 仅 askAgent 回复 + 失败回通用文案(不回内部诊断,安全补钉);
 * 非白名单(含缺省仅 owner) = 固定拒答文案 + 零 LLM 调用(返回 'rejected')。
 * bot 需提供 reply(msg, text)/sendTyping(userId)；queue 提供 run(task)。 */
export async function handleMessage(text, msg, bot, queue, deps = {}) {
  if (!text?.trim()) return null
  const isOwner = msg.userId === OWNER_ID
  // F-SEC-03 (#316): 白名单门置顶于 C1 之前 —— 非 owner 必须命中白名单才放行，
  // 否则直接拒答固定文案且零 LLM 调用（成本攻击无门槛封闭）。owner 恒放行。
  const isAllowed = isAllowedContact(msg.userId, deps.whitelist)
  const repoRoot = deps.repoRoot ?? REPO_ROOT
  const askAgentFn = deps.askAgent ?? askAgent
  const extractAgent = deps.extractAgent ?? defaultExtractAgent
  const verifyAgent = deps.verifyAgent ?? defaultVerifyAgent
  const runDaemon = deps.runDaemon ?? defaultRunDaemon
  const now = deps.now ?? (() => new Date())

  // ── 白名单门:非白名单(含缺省=仅 owner)→ 固定拒答文案,不调 askChat/askAgent(零 LLM 成本) ──
  if (!isAllowed) {
    await bot.reply(msg, REJECT_TEXT).catch(() => console.warn('[whitelist reject] 拒答回复发送失败'))
    return 'rejected'
  }

  // ── C1 门:非 owner(仅白名单内)不进写/回忆/追问/特殊命令路径,不取 --attention;仅 askAgent 回复 ──
  if (!isOwner) {
    await askChat(text, msg, bot, queue, askAgentFn)
    return 'agent'
  }

  // U5 (#233, D1): 每条主人消息本地生成 recv-id，recordUserMsg 与 upgradeAnalysis
  // 同传 → daemon recv_dedup 按 id 精确判定补报升级（免 450s 窗口；仅去重流，不进 agent prompt）
  const recvId = randomUUID()
  await queue.run(() => recordUserMsg(text, recvId))   // 确定性回传 daemon(命令消息 = --user-msg 无分析,dedup 继承)

  // 澄清检查:有待澄清记录且非退出词 → 路由回提取(词表命中=新命令;否则合并"原意+回答")
  // B3 兜底:readClarify 内部 rmSync 已容错,防御性再包一层——记录损坏/目录占位等极端情况不阻塞回复流
  let clarify = null
  try { clarify = readClarify(repoRoot) } catch { clarify = null }
  let exitWord = false
  if (clarify && !exitWordMatch(text)) {
    const wordIntent = detectScheduleIntent(text)          // 词表命中 = 新命令
    const isNewCommand = wordIntent && wordIntent.intent !== 'extract'
    const original = isNewCommand ? text : `${clarify.original}\n(补充回答:${text})`
    const released = await queue.run(() => handleScheduleCommand(original, msg, bot,
      { ...deps, repoRoot, clarify, extractAgent, verifyAgent, runDaemon, now }))
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
        // #192: /new 先重启常驻 agent(await 子进程退出、释放旧会话文件),再备份会话文件——
        // 否则备份的是旧进程仍持有的会话文件,时序错乱。
        if (slash.action === 'new_session' && AGENT_RPC_ENABLED && globalThis.__agentRpc) {
          await globalThis.__agentRpc.restart()
          console.log('[slash] agent-rpc 已重启(新会话)')
        }
        const r = await executeSlashCommand(spawn, slash, BRIDGE_DIR)
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

  // 特殊命令（纪念日/假期）确定性接管：命中则直接执行 daemon，不经 agent（Phase 4 Task 14）
  const special = detectSpecialCommand(text)
  if (special) {
    await queue.run(async () => {
      try {
        const r = await executeSpecialCommand(spawn, special, DAEMON_PY, DAEMON_SCRIPT)
        console.log(`[special] ${special.action} → ok=${r.ok}`)
        await bot.reply(msg, r.reply)
      } catch (err) {
        const raw = err instanceof Error ? err.message : String(err)
        const reason = sanitizeError(raw, text)
        console.error('[special error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason}`).catch(() => {})
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
        // 回复侧:先取 --attention + --memory-search 注入(失败降级,§5.4),analysis 带 recall 信号 → 第二趟 agent
        const att = await getAttention()
        const mem = await getMemories(text)
        const { text: reply, analysis } = await askAgentWithAttention(text, att, mem)
        if (analysis?.recall) {
          // B7: 复用主链已有 analysis,runWithRecall 不再重复完整 firstAnalysis
          // R4: 用已解析的 askAgentFn(deps.askAgent 注入优先)——硬编码模块级 askAgent 会绕过注入
          const second = await runWithRecall(text, askAgentFn, analysis)
          if (second) {
            // B8: recalled 分支同样 upgradeAnalysis——daemon 侧该消息才能以
            // recv_dedup 升级语义记账（--user-msg 带 --analysis），与未命中路径一致
            await upgradeAnalysis(text, analysis, recvId)
            await bot.reply(msg, second).catch((e) => console.error('[reply error]', e))
            recalled = true
          }
        }
        if (!recalled) {
          await upgradeAnalysis(text, analysis, recvId)  // recv_dedup 升级语义，不重复记账
          console.log(`[out] ${(reply ?? '').length} chars`)
          await bot.reply(msg, reply).catch((e) => console.error('[reply error]', e))
        }
      } catch (err) {
        const raw = err instanceof Error ? err.message : String(err)
        const reason = sanitizeError(raw, text)
        console.error('[agent error]', reason)
        await bot.reply(msg, `⚠️ 处理失败：${reason}`).catch(() => {})
        await recordAgentHealth(bot, 'fail', raw)
        return
      }
      // 回复失败不记 agent 假死（发送故障 ≠ agent 故障）；记账在回复后执行，不延迟当前消息
      await recordAgentHealth(bot, 'success')
    })
    .catch((err) => console.error('[queue error]', err))
  return 'agent'
}
