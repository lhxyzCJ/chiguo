import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { resolveRepo } from '../scripts/agent-run.mjs'
const execFileP = promisify(execFile)
const REPO = resolveRepo(import.meta.url)
const AGENT_HEALTH_SCRIPT = process.env.WECHAT_BRIDGE_AGENT_HEALTH ?? new URL('../scripts/agent_health.py', import.meta.url).pathname
const AGENT_HEALTH_PY = process.env.WECHAT_BRIDGE_AGENT_HEALTH_PY ?? `${REPO}/.venv/bin/python`
const OWNER_ID = process.env.WECHAT_BRIDGE_OWNER ?? 'owner@im.wechat'
export async function recordAgentHealth(bot, outcome, reason = null) {
  try {
    const args = [AGENT_HEALTH_SCRIPT, 'record', '--outcome', outcome]
    if (reason) args.push('--reason', String(reason).slice(0, 100))
    const { stdout } = await execFileP(AGENT_HEALTH_PY, args, { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 })
    const parsed = JSON.parse(stdout)
    if (parsed.transition !== 'none' && parsed.message) {
      await bot.send(OWNER_ID, parsed.message).catch((e) => console.error('[agent health alert send error]', e instanceof Error ? e.message : String(e)))
    }
    return parsed
  } catch (err) {
    console.error('[agent health record error]', err instanceof Error ? err.message : String(err))
    return null
  }
}
