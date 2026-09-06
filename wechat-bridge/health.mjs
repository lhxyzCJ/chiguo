/** wechat-bridge/health.mjs — agent 假死记账（agent_health.py 状态机）。
 * 以 bridge.mjs 内联实现为准选主（healthRecordArgs DTO 白名单版），旧影子手拼版已删。 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { AGENT_HEALTH_SCRIPT, AGENT_HEALTH_PY, OWNER_ID } from './env.mjs'
import { healthRecordArgs } from './cli-dto.mjs'

const execFileP = promisify(execFile)

/** agent 假死记账：askAgent/agent-run 成败记录进 agent_health 状态机（零额外 agent 调用）。
 * transition=down/up 时向 OWNER_ID 发告警/恢复消息。整体绝不抛错、绝不影响回复流。 */
export async function recordAgentHealth(bot, outcome, reason = null) {
  try {
    // #391: outcome 白名单校验（fail/send_fail/success），非法 → 记错日志返回 null
    const args = [AGENT_HEALTH_SCRIPT, ...healthRecordArgs(outcome, reason)]
    const { stdout } = await execFileP(AGENT_HEALTH_PY, args, {
      timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024,
    })
    const parsed = JSON.parse(stdout)
    if (parsed.transition !== 'none' && parsed.message) {
      await bot.send(OWNER_ID, parsed.message)
        .catch((e) => console.error('[agent health alert send error]',
          e instanceof Error ? e.message : String(e)))
    }
    return parsed
  } catch (err) {
    console.error('[agent health record error]',
      err instanceof Error ? err.message : String(err))
    return null
  }
}
