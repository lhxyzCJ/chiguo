#!/usr/bin/env node
/**
 * tests/fake-agent-rpc.mjs — 模拟 pi --mode rpc：stdin JSON 命令 → stdout NDJSON 事件。
 * 契约（pi dist/modes/rpc）：get_state 回 response；prompt 先回 preflight response
 * （success:true），再流式输出 message_end（含 text/usage）与 agent_settled（回合完成）。
 * 供 test_agent_rpc.mjs 注入（AgentRpc({ bin: process.execPath, args: [本文件] })）。
 */
import readline from 'node:readline'

const rl = readline.createInterface({ input: process.stdin })
rl.on('line', (line) => {
  let cmd
  try { cmd = JSON.parse(line) } catch { return }
  if (cmd.type === 'get_state') {
    process.stdout.write(JSON.stringify({ type: 'response', id: cmd.id, command: 'get_state', success: true }) + '\n')
  } else if (cmd.type === 'prompt') {
    // preflight 即回（与真实 pi 一致：response 在 preflight 即回，回合完成以 agent_settled 为准）
    process.stdout.write(JSON.stringify({ type: 'response', id: cmd.id, command: 'prompt', success: true }) + '\n')
    setTimeout(() => {
      const text = '<<ANALYSIS>>{"warmth":0.5,"effort":0.6,"attention":0.7}<<END>> 测试回复'
      process.stdout.write(JSON.stringify({
        type: 'message_end',
        message: { content: [{ type: 'text', text }], usage: { prompt_tokens: 10, completion_tokens: 5 } },
      }) + '\n')
      process.stdout.write(JSON.stringify({ type: 'agent_settled' }) + '\n')
    }, 20)
  }
})
