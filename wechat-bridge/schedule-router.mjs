import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { DAEMON_PY, DAEMON_SCRIPT, AGENT_RUN_SCRIPT, REPO_ROOT } from './daemon-client.mjs'
const execFileP = promisify(execFile)
async function withTimeout(p, ms) {
  let timer
  try { return await Promise.race([p, new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`timeout ${ms}ms`)), ms) })]) } finally { clearTimeout(timer) }
}
export function makeScheduleDeps(repoRoot) {
  return {
    async extractAgent(original) {
      const att = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--attention'], { timeout: 30_000 }).catch(() => ({ stdout: '{}' }))
      let attention = {}
      try { attention = JSON.parse(att.stdout) } catch {}
      const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, '--prompt', original, '--schedule-extract', '--attention', JSON.stringify(attention), '--week-num', String(attention.week_num ?? 1)], { timeout: 180_000 })
      const res = JSON.parse(stdout)
      return res.parsed ?? { ok: false, error: 'no block' }
    },
    async verifyAgent(item, original) {
      const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, '--prompt', original, '--schedule-verify', '--item', JSON.stringify(item)], { timeout: 180_000 })
      return JSON.parse(stdout).parsed ?? { ok: false }
    },
    async runDaemon(item) {
      const { stdout } = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--schedule-change', JSON.stringify(item)], { timeout: 30_000 })
      return JSON.parse(stdout)
    },
  }
}
let scheduleDeps = null
function scheduleDefaults() {
  scheduleDeps ??= makeScheduleDeps(REPO_ROOT)
  return scheduleDeps
}
export const defaultExtractAgent = (original) => scheduleDefaults().extractAgent(original)
export const defaultVerifyAgent = (item, original) => scheduleDefaults().verifyAgent(item, original)
export const defaultRunDaemon = (item) => scheduleDefaults().runDaemon(item)
export { withTimeout }
