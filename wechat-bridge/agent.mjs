/** wechat-bridge/agent.mjs — agent 调用层（askAgent：RPC 常驻优先 → spawn 回退）。
 * 依赖 env（常量）+ util。调用方（message.mjs）注入 TurnQueue 串行。 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { existsSync } from 'node:fs'
import { AGENT_RUN_SCRIPT, AGENT_RPC_ENABLED } from './env.mjs'

const execFileP = promisify(execFile)

/** argv DTO：--prompt 原文 + --analysis-mode（一次完成情绪分析 JSON + 回复）。 */
function agentAnalysisArgs(text) {
  if (typeof text !== 'string' || !text) throw new TypeError('prompt 必须是非空字符串')
  return ['--prompt', text, '--analysis-mode']
}

/** U8c: AGENT_RUN_SCRIPT 启动校验（导出供启动期调用，不启动 WeChatBot）。
 * 返回 null = 通过；返回错误文案 = 启动时明确报错（替代 ask 期通用失败文案，
 * 便于诊断「env 未配置 / scripts/agent-run.mjs 缺失或被误删」两种配置问题）。 */
export function checkAgentRunScript(script) {
  if (!script || typeof script !== 'string' || script.length === 0) {
    return 'AGENT_RUN_SCRIPT 未配置:WECHAT_BRIDGE_AGENT_RUN 为空或未设置(通过 wechat-bridge.sh 启动会自动注入,或手动 export WECHAT_BRIDGE_AGENT_RUN=<repo>/scripts/agent-run.mjs)'
  }
  if (!existsSync(script)) {
    return `AGENT_RUN_SCRIPT 指向的脚本不存在: ${script}(请检查 WECHAT_BRIDGE_AGENT_RUN 配置,或确认 scripts/agent-run.mjs 已随仓库部署未被误删)`
  }
  return null
}

/** 调用 pi-agent（agent-run.mjs），一次完成「情绪分析 JSON + 回复」。
 * 返回 { text, analysis }；analysis 为解析后的对象或 null。失败抛错。 */
export async function askAgent(text) {
  // RPC 常驻优先:失败 → 回退 spawn(agent-rpc 抛错即回退)
  if (AGENT_RPC_ENABLED) {
    try {
      const { AgentRpc } = await import('./agent-rpc.mjs')
      if (!globalThis.__agentRpc) globalThis.__agentRpc = new AgentRpc()
      const r = await globalThis.__agentRpc.prompt(text)
      return { text: r.text, analysis: r.analysis ?? null }
    } catch (err) {
      console.error('[agent-rpc] 失败,回退 spawn:', err instanceof Error ? err.message : String(err))
    }
  }
  const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, ...agentAnalysisArgs(text)], {
    timeout: 180_000,
    maxBuffer: 16 * 1024 * 1024,
  })

  let parsed
  try {
    parsed = JSON.parse(stdout)
  } catch {
    throw new Error(`agent-run 输出非 JSON: ${String(stdout).slice(0, 100)}`)
  }
  if (!parsed.ok) {
    throw new Error(parsed.error ?? 'agent-run 返回 ok=false 且无 error')
  }
  return { text: parsed.text, analysis: parsed.analysis ?? null }
}
