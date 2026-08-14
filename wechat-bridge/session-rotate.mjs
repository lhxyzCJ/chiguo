#!/usr/bin/env node
/**
 * session-rotate — 每日固定时间自动轮换 ai 会话（防上下文无限堆积）
 *
 * 背景：chiguo-main（回复链）/ chiguo-send（主动发送链）会话文件随轮次无限增长，
 * prompt tokens 达 ~10 万后需手动 /new（doc/微信命令.md）。本模块提供每日固定时刻
 * （默认 04:00 CST）自动轮换：与 /new 共享同一备份逻辑（backupSessionFile），
 * RPC 常驻模式下先重启 agent 会话（#192 时序：先杀进程释放会话文件，再备份）再备份。
 *
 * 幂等性：`~/.chiguo/session-rotate-last` 记录最近一次轮换的 CST 日期，同一天只轮换
 * 一次；bridge 重启/宕机错过时刻 → 下次启动补轮换（标记陈旧即补，无需等第二天）。
 *
 * 配置（env，由 bridge.mjs 读取）：
 *   WECHAT_BRIDGE_SESSION_ROTATE=0           关闭（默认开启）
 *   WECHAT_BRIDGE_SESSION_ROTATE_TIME=HH:MM  轮换时刻（CST，默认 04:00）
 */
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { backupSessionFile } from './command-detect.mjs'

export const CST_OFFSET_MS = 8 * 3600 * 1000

/** now + 8h → 用 UTC 字段表达 CST 日历（与 command-detect cstNow 同款）。 */
export function cstNow(now = new Date()) {
  return new Date(now.getTime() + CST_OFFSET_MS)
}

/** CST 日期串 YYYY-MM-DD。 */
export function cstDateStr(now = new Date()) {
  return cstNow(now).toISOString().slice(0, 10)
}

/** "HH:MM" → {h, m}；非法（非数字/越界/格式错）→ null。 */
export function parseRotateTime(timeStr) {
  const m = /^(\d{1,2}):([0-5]\d)$/.exec(String(timeStr ?? '').trim())
  if (!m) return null
  const h = Number(m[1])
  if (h > 23) return null
  return { h, m: Number(m[2]) }
}

/** 今天（CST）轮换时刻对应的 UTC ms；timeStr 非法 → NaN。 */
export function rotateInstantUTC(now = new Date(), timeStr) {
  const t = parseRotateTime(timeStr)
  if (!t) return NaN
  const c = cstNow(now)
  return Date.UTC(c.getUTCFullYear(), c.getUTCMonth(), c.getUTCDate(), t.h - 8, t.m)
}

/** 距下一次轮换时刻的 ms（今天未到 → 今天；已过/正到 → 明天）；非法 → NaN。 */
export function msToNextRotate(now = new Date(), timeStr) {
  const inst = rotateInstantUTC(now, timeStr)
  if (Number.isNaN(inst)) return NaN
  const t = now.getTime()
  return (inst > t ? inst : inst + 24 * 3600e3) - t
}

/** 读最近轮换日期标记（无/损坏 → null）。 */
export function readLastRotate(markerPath) {
  try {
    const s = readFileSync(markerPath, 'utf8').trim()
    return /^\d{4}-\d{2}-\d{2}$/.test(s) ? s : null
  } catch {
    return null
  }
}

export function writeLastRotate(markerPath, date) {
  mkdirSync(dirname(markerPath), { recursive: true })
  writeFileSync(markerPath, date)
}

/** 轮换两个会话（main + send）：RPC 常驻先杀进程（#192：释放会话文件），再备份。
 *  返回 { main, send } = 备份路径（无对应会话文件 → null）。rpc 可为 null/无 restart。 */
export async function rotateSessions({ cwd, backupsDir, rpc = null }) {
  if (rpc?.restart) await rpc.restart()
  const main = backupSessionFile(cwd, backupsDir, 'chiguo-main')
  const send = backupSessionFile(cwd, backupsDir, 'chiguo-send')
  return { main, send }
}

/** 到点且当日未轮换 → 轮换并写标记；返回 { main, send }，未轮换 → false。 */
export async function rotateIfDue({ now = new Date(), timeStr, markerPath, backupsDir, cwd, rpc = null }) {
  if (readLastRotate(markerPath) === cstDateStr(now)) return false
  const inst = rotateInstantUTC(now, timeStr)
  if (Number.isNaN(inst) || now.getTime() < inst) return false
  const out = await rotateSessions({ cwd, backupsDir, rpc })
  writeLastRotate(markerPath, cstDateStr(now))
  return out
}

/** 默认路径（生产）：备份目录 + 幂等标记，均锚定 ~/.chiguo/。 */
export function defaultRotatePaths() {
  return {
    backupsDir: join(homedir(), '.chiguo', 'session-backups'),
    markerPath: join(homedir(), '.chiguo', 'session-rotate-last'),
  }
}
