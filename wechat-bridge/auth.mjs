import { HOST } from '../scripts/agent-run.mjs'
export const BRIDGE_TOKEN = process.env.WECHAT_BRIDGE_TOKEN
export const OWNER_ID = process.env.WECHAT_BRIDGE_OWNER ?? 'owner@im.wechat'
export const REJECT_TEXT = process.env.WECHAT_BRIDGE_WHITELIST_REJECT ?? '这是迟菓的私人助手，暂不对陌生人开放哦'
function resolveWhitelist() {
  const env = process.env.WECHAT_BRIDGE_WHITELIST
  if (env) return env.split(',').map((s) => s.trim()).filter(Boolean)
  const wl = HOST.whitelist_contacts
  return Array.isArray(wl) ? wl : []
}
export const WHITELIST = resolveWhitelist()
export function isAllowedContact(userId, whitelist = WHITELIST, ownerId = OWNER_ID) {
  if (userId === ownerId) return true
  return whitelist.includes(userId)
}
export function isLocalHost(host) {
  if (!host) return false
  const h = String(host).toLowerCase().replace(/:\d+$/, '')
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]'
}
export function isLocalOrigin(origin) {
  if (!origin) return true
  try {
    const u = new URL(origin)
    return u.protocol === 'http:' && isLocalHost(u.host)
  } catch { return false }
}
