#!/usr/bin/env node
/**
 * session-rotate — 主会话（chiguo-main）每日轮换：每小时检查，空闲超阈值才轮换
 *
 * 语义（详见 doc/SYSTEM.md §11.2）：
 * - 每小时整点检查一次（每天首个检查点 = 00:00 CST，正常情况轮换落在凌晨）
 * - 距最近活动（用户消息 / cron 判定要发消息，写于 ~/.chiguo/session-activity-last）
 *   超过 session_rotate_idle_minutes（默认 60）才轮换 → 绝不切断进行中的对话；
 *   有活动则顺延到下一检查点（深夜连续对话可能推迟到清晨）
 * - 轮换 = RPC 常驻先杀进程（#192 时序）→ 备份 chiguo-main → 开新会话；
 *   幂等标记 ~/.chiguo/session-rotate-last（同日只轮换一次）
 * - send 会话（chiguo-send）不在此轮换：它每轮全新（bridge /agent/prompt send 前轮换 +
 *   agent-run.mjs AGENTRUN_ROTATE_SESSION=1 兜底），上下文恒 ≤1 轮
 *
 * 活动文件格式：epoch 秒（整数），由 bridge（onMessage 生产入口）与 chiguo-tick.sh
 * （ACTION=send 判定）写入；文件缺失视为空闲（允许轮换）。
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

/** 今天（CST）00:00 对应的 UTC ms（msToNextCheck 网格基准用）。 */
export function dayStartUTC(now = new Date()) {
  const c = cstNow(now)
  return Date.UTC(c.getUTCFullYear(), c.getUTCMonth(), c.getUTCDate(), -8, 0)
}

/** 距下一个检测网格边界（整点对齐 checkMinutes）的 ms；非法/过小 → 退避 60s。 */
export function msToNextCheck(now = new Date(), checkMinutes = 60) {
  const m = Number(checkMinutes)
  if (!Number.isFinite(m) || m < 5) return 60_000
  const c = cstNow(now)
  const dayMs = ((c.getUTCHours() * 60 + c.getUTCMinutes()) * 60 + c.getUTCSeconds()) * 1000 + c.getUTCMilliseconds()
  const step = m * 60_000
  const nextDayMs = Math.floor(dayMs / step) * step + step
  return dayStartUTC(now) + nextDayMs - now.getTime()
}

/** 读活动时间戳（epoch 秒；无/损坏 → null=视为空闲）。 */
export function readActivity(activityPath) {
  try {
    const s = readFileSync(activityPath, 'utf8').trim()
    const n = Number(s)
    return Number.isFinite(n) && n > 0 ? n : null
  } catch {
    return null
  }
}

export function writeActivity(activityPath, tsSeconds = Math.floor(Date.now() / 1000)) {
  mkdirSync(dirname(activityPath), { recursive: true })
  writeFileSync(activityPath, String(tsSeconds))
}

/** 距最近活动是否已超过 idleMinutes 分钟（活动缺失 → 空闲）。 */
export function isIdleSince(now = new Date(), activitySeconds, idleMinutes = 60) {
  if (activitySeconds == null) return true
  return (now.getTime() / 1000 - activitySeconds) >= Number(idleMinutes) * 60
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

/** 当日未轮换且空闲超阈值 → 轮换主会话并写标记。
 *  返回 { main }（备份路径，无会话文件 → null）已轮换；
 *  false=今日已轮换；'active'=近期有活动（顺延到下一检查点）。
 *  rpc 可为 null/无 restart（非 RPC 模式直接备份）。 */
export async function rotateIfDue({ now = new Date(), markerPath, backupsDir, cwd, rpc = null, activityPath = null, idleMinutes = 60 }) {
  if (readLastRotate(markerPath) === cstDateStr(now)) return false
  const act = activityPath ? readActivity(activityPath) : null
  if (!isIdleSince(now, act, idleMinutes)) return 'active'
  if (rpc?.restart) await rpc.restart()
  const main = backupSessionFile(cwd, backupsDir, 'chiguo-main')
  writeLastRotate(markerPath, cstDateStr(now))
  return { main }
}

/** 默认路径（生产）：备份目录 + 幂等标记 + 活动时间戳，均锚定 ~/.chiguo/。 */
export function defaultRotatePaths() {
  return {
    backupsDir: join(homedir(), '.chiguo', 'session-backups'),
    markerPath: join(homedir(), '.chiguo', 'session-rotate-last'),
    activityFile: join(homedir(), '.chiguo', 'session-activity-last'),
  }
}
