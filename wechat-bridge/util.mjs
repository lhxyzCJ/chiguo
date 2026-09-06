/** wechat-bridge/util.mjs — 无依赖小工具（withTimeout + sanitizeError）。
 * 被 send/agent/schedule/message 共用，零内部依赖，防循环 import。 */

/** 脱敏：错误消息中若含完整 prompt 明文（长串），替换为长度标记后截断 100，确保不回显全文 */
export function sanitizeError(reason, payloadText) {
  let r = String(reason ?? '')
  if (payloadText && typeof payloadText === 'string' && payloadText.length > 20) {
    if (r.includes(payloadText)) {
      r = r.split(payloadText).join(`[prompt ${payloadText.length} chars]`)
    } else {
      // 部分包含（如错误只含 prompt 前缀 30 字符）：检测最长公共前缀
      const head = payloadText.slice(0, 30)
      if (head && r.includes(head)) {
        r = r.split(head).join('[prompt redacted]')
      }
    }
  }
  return r.slice(0, 100)
}

export async function withTimeout(p, ms) {
  let timer
  try {
    return await Promise.race([
      p,
      new Promise((_, rej) => { timer = setTimeout(() => rej(new Error(`timeout ${ms}ms`)), ms) }),
    ])
  } finally { clearTimeout(timer) }
}
