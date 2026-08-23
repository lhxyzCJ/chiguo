import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { dirname } from 'node:path'
import { existsSync } from 'node:fs'
import { resolveRepo, RUNNER } from '../scripts/agent-run.mjs'
const execFileP = promisify(execFile)
export const REPO = resolveRepo(import.meta.url)
export const BRIDGE_DIR = `${REPO}/wechat-bridge`
export const DAEMON_PY = process.env.WECHAT_BRIDGE_DAEMON_PY ?? `${REPO}/.venv/bin/python`
export const DAEMON_SCRIPT = process.env.WECHAT_BRIDGE_DAEMON ?? `${REPO}/chiguo_daemon.py`
export const REPO_ROOT = dirname(DAEMON_SCRIPT)
export const AGENT_RUN_SCRIPT = process.env.WECHAT_BRIDGE_AGENT_RUN ?? new URL('../scripts/agent-run.mjs', import.meta.url).pathname
export const AGENT_RPC_ENABLED = RUNNER === 'agent' && process.env.WECHAT_BRIDGE_AGENT_RPC === '1'
export const SEND_TIMEOUT_MS = Number(process.env.WECHAT_BRIDGE_SEND_TIMEOUT_MS ?? 30_000)
export const SEND_PROMPT_TOTAL_MS = Number(process.env.WECHAT_BRIDGE_SEND_PROMPT_TOTAL_MS ?? 110_000)
export const SEND_PROMPT_QUEUE_WAIT_MS = Number(process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS ?? 30_000)
export function checkAgentRunScript(script) {
  if (!script || typeof script !== 'string' || script.length === 0) return 'AGENT_RUN_SCRIPT 未配置:WECHAT_BRIDGE_AGENT_RUN 为空或未设置'
  if (!existsSync(script)) return `AGENT_RUN_SCRIPT 指向的脚本不存在: ${script}`
  return null
}
export async function getAttention() {
  try {
    const r = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--attention'], { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 })
    return JSON.parse(r.stdout)
  } catch { return null }
}
export async function getMemories(query) {
  try {
    const r = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--memory-search', query], { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 })
    return JSON.parse(r.stdout)
  } catch { return null }
}
export async function recordUserMsg(text, recvId) {
  const args = [DAEMON_SCRIPT, '--user-msg', text]
  if (recvId) args.push('--recv-id', recvId)
  try { await execFileP(DAEMON_PY, args, { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 }) }
  catch (err) { console.error('[user-msg record error]', err instanceof Error ? err.message : String(err)) }
}
export async function upgradeAnalysis(text, analysis, recvId) {
  if (!analysis) return
  const analysisJson = typeof analysis === 'string' ? analysis : JSON.stringify(analysis)
  const args = [DAEMON_SCRIPT, '--user-msg', text, '--analysis', analysisJson]
  if (recvId) args.push('--recv-id', recvId)
  try { await execFileP(DAEMON_PY, args, { timeout: 30_000, maxBuffer: 4 * 1024 * 1024 }) }
  catch (err) { console.error('[analysis upgrade error]', err instanceof Error ? err.message : String(err)) }
}
export async function askAgent(text) {
  if (AGENT_RPC_ENABLED) {
    try {
      const { AgentRpc } = await import('./agent-rpc.mjs')
      if (!globalThis.__agentRpc) globalThis.__agentRpc = new AgentRpc()
      const r = await globalThis.__agentRpc.prompt(text)
      return { text: r.text, analysis: r.analysis ?? null }
    } catch (err) { console.error('[agent-rpc] 失败,回退 spawn:', err instanceof Error ? err.message : String(err)) }
  }
  const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, '--prompt', text, '--analysis-mode'], { timeout: 180_000, maxBuffer: 16 * 1024 * 1024 })
  let parsed
  try { parsed = JSON.parse(stdout) } catch { throw new Error(`agent-run 输出非 JSON: ${String(stdout).slice(0, 100)}`) }
  if (!parsed.ok) throw new Error(parsed.error ?? 'agent-run 返回 ok=false 且无 error')
  return { text: parsed.text, analysis: parsed.analysis ?? null }
}
export function sanitizeError(reason, payloadText) {
  let r = String(reason ?? '')
  if (payloadText && typeof payloadText === 'string' && payloadText.length > 20) {
    if (r.includes(payloadText)) r = r.split(payloadText).join(`[prompt ${payloadText.length} chars]`)
    else {
      const head = payloadText.slice(0, 30)
      if (head && r.includes(head)) r = r.split(head).join('[prompt redacted]')
    }
  }
  return r.slice(0, 100)
}
