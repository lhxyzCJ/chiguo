/** wechat-bridge/send.mjs — HTTP 发送端点（POST /send + /agent/prompt 同 server 同鉴权）。
 * 从 bridge.mjs 纯搬运；鉴权中间件（Content-Type→Origin→Host→token→1M 上限）整体搬运，不得拆散。
 * 依赖 env + agent（handleAgentPrompt）+ util。 */
import { createServer } from 'node:http'
import { SEND_PORT, SEND_TIMEOUT_MS, BRIDGE_TOKEN, OWNER_ID, isLocalHost, isLocalOrigin } from './env.mjs'
import { handleAgentPrompt } from './agent.mjs'
import { withTimeout } from './util.mjs'

/** 主动发送端点：POST /send {"to","text"} → bot.send()；POST /agent/prompt {"text","mode"} → AgentRpc。
 *  仅本地回环来源(#84 鉴权)+ 可选 token。 */
export function startSendServer(bot, queue) {
  const server = createServer((req, res) => {
    res.on('error', () => {})  // 客户端断开后 res 被销毁 → 吸收 error 事件，防未处理异常
    const deny = (status, error) => {
      if (res.writableEnded || res.destroyed) return
      res.writeHead(status, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify({ ok: false, error }))
    }
    if (req.method !== 'POST' || (req.url !== '/send' && req.url !== '/agent/prompt')) {
      deny(405, 'only POST /send or /agent/prompt')
      return
    }
    // #84 鉴权:Content-Type JSON → 本地来源(Host/Origin) → 共享 token
    // （#191: 未配置 token 时 main() FATAL 拒绝启动，此处 unreachable 防御保留无妨）
    if (!String(req.headers['content-type'] ?? '').toLowerCase().includes('application/json')) {
      deny(415, 'Content-Type must be application/json')
      return
    }
    if (!isLocalOrigin(req.headers.origin)) { deny(403, 'forbidden origin'); return }
    if (!isLocalHost(req.headers.host)) { deny(403, 'forbidden host'); return }
    if (BRIDGE_TOKEN && req.headers['x-bridge-token'] !== BRIDGE_TOKEN) {
      deny(403, 'forbidden token')
      return
    }
    let body = ''
    let oversize = false
    req.on('data', (c) => {
      if (oversize) return
      body += c
      if (Buffer.byteLength(body, 'utf8') > 1_000_000) {
        oversize = true
        if (!res.writableEnded && !res.destroyed) deny(413, 'payload too large')
        req.destroy()
      }
    })
    req.on('error', () => {})  // destroy 后连接重置，避免未处理错误事件
    req.on('end', async () => {
      if (oversize) return
      // #84 参数校验:非法 JSON / 必填缺失 → 400(而非 500)
      let payload
      try { payload = JSON.parse(body || '{}') } catch { deny(400, 'invalid JSON'); return }
      if (req.url === '/agent/prompt') {
        await handleAgentPrompt(payload, res, queue)
        return
      }
      const resp = await sendMessage(payload, bot)
      if (!res.writableEnded && !res.destroyed) {
        res.writeHead(resp.status, { 'Content-Type': 'application/json' })
        res.end(JSON.stringify({ ok: resp.ok, ...(resp.error !== undefined && { error: resp.error }),
                                 ...(resp.timeout_uncertain && { timeout_uncertain: true }) }))
      }
    })
  })
  // #84: listen 失败(端口占用等)→ 打印并退出,不留僵尸服务
  server.on('error', (err) => {
    console.error('[send server error]', err instanceof Error ? err.message : String(err))
    process.exit(1)
  })
  server.listen(SEND_PORT, '127.0.0.1', () => {
    console.log(`send server: http://127.0.0.1:${SEND_PORT}/send`)
  })
}

/** /send 发送处理器（从 startSendServer 提取，可独立单元测试）。
 * 校验 → bot.send 超时兜底 → 返回响应对象 {status, ok, error?, timeout_uncertain?}。
 * F-A17-003：bot.send 经 withTimeout 超时不代表未送达 → 超时路径返回
 * `timeout_uncertain: true`，调用方（tick/loop）必须区别处理（不退款不重发），
 * 不能当明确失败。明确失败（非超时异常）只置 ok:false + error，无该标记；
 * prepare failed（context_token 过期）走既有显式恢复提示，保持 ok:false 兼容。 */
export async function sendMessage(payload, bot) {
  const { to, text } = payload ?? {}
  if (typeof to !== 'string' || !to.trim()) return { status: 400, ok: false, error: 'to 必填' }
  if (typeof text !== 'string' || !text.trim()) return { status: 400, ok: false, error: 'text 必填' }
  if (to !== OWNER_ID) return { status: 403, ok: false, error: 'forbidden recipient' }
  // #224: 服务端拒发 "prepare failed" = context_token 过期（微信侧无公开 TTL，
  // 实测最后一次收到用户消息后约 35h 失效；每次收到用户消息自动刷新）。
  // 显式提示恢复路径，避免误判为网络/登录问题而盲目重扫码。
  const isStaleToken = (reason) => reason.includes('prepare failed')
  const isTimeout = (reason) => /^timeout \d+ms$/.test(reason)  // withTimeout 的 reject 消息
  try {
    await withTimeout(bot.send(to, text), SEND_TIMEOUT_MS)   // #84 send 超时兜底,不挂死请求
    console.log(`[send] ${to}: ${text.length} chars`)  // 脱敏：不落正文（仅长度）
    return { status: 200, ok: true }
  } catch (err) {
    const reason = err instanceof Error ? err.message : String(err)
    if (isStaleToken(reason)) {
      console.error(`[send error] ${reason}（context_token 过期：从微信给机器人发一条消息即刷新恢复，无需重新扫码）`)
    } else {
      console.error(`[send error] ${reason}`)
    }
    // F-A17-003: 超时不确定 → 带 timeout_uncertain 标记（调用方别当明确失败退款）
    if (isTimeout(reason)) {
      return { status: 500, ok: false, error: reason, timeout_uncertain: true }
    }
    return { status: 500, ok: false, error: reason }
  }
}
