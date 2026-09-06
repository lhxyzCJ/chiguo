/** wechat-bridge/schedule.mjs — 调度链路（澄清存取 + 命令提取/校验/写入 + 会话轮换）。
 * 从 bridge.mjs 纯搬运；依赖 env + cli-dto + session-rotate + util。
 * REPO_ROOT 锚连 scheduleClarifyPath 默认参数一起走（测试隔离依赖 WECHAT_BRIDGE_DAEMON 覆盖）。 */
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { existsSync, readFileSync, writeFileSync, chmodSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { msToNextCheck, rotateIfDue, defaultRotatePaths, cstDateStr } from './session-rotate.mjs'
import { agentExtractArgs, agentVerifyArgs, daemonScheduleChangeArgs } from './cli-dto.mjs'
import { AGENT_RUN_SCRIPT, DAEMON_PY, DAEMON_SCRIPT, REPO_ROOT, ROTATE_CFG, BRIDGE_DIR } from './env.mjs'
import { withTimeout } from './util.mjs'

const execFileP = promisify(execFile)

// ── schedule-center 6a:澄清记录(仓库根锚定 = DAEMON_SCRIPT 父目录;A3)──
export function scheduleClarifyPath(repoRoot = REPO_ROOT) { return join(repoRoot, 'schedule_clarify.json') }
export function readClarify(repoRoot = REPO_ROOT) {
  const p = scheduleClarifyPath(repoRoot)
  if (!existsSync(p)) return null
  try {
    const rec = JSON.parse(readFileSync(p, 'utf8'))
    if (!rec.expires_at || Date.parse(rec.expires_at) <= Date.now()) {  // 6h 过期静默清理
      rmSync(p, { force: true, recursive: true })   // B3: 路径被目录占据时 ERR_FS_EISDIR 也会被消化
      return null
    }
    return rec
  } catch {  // 损坏 → 清空为无记录
    rmSync(p, { force: true, recursive: true })     // B3
    return null
  }
}
export function writeClarify(repoRoot, rec) {
  const p = scheduleClarifyPath(repoRoot)
  const tmp = p + '.tmp'
  writeFileSync(tmp, JSON.stringify(rec), { mode: 0o600 })
  chmodSync(tmp, 0o600)
  renameSync(tmp, p)
}
export function clearClarify(repoRoot = REPO_ROOT) { rmSync(scheduleClarifyPath(repoRoot), { force: true, recursive: true }) }  // B3
export function exitWordMatch(text) { return /^(?:算了|不要了|没事)/.test(text.trim()) }

/** 命令链路默认实现:agent-run 模式 + daemon CLI(独立会话 chiguo-extract/verify;A4 shape 直读 stdout JSON) */
export function makeScheduleDeps(repoRoot) {
  return {
    async extractAgent(original) {
      const att = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, '--attention'], { timeout: 30_000 }).catch(() => ({ stdout: '{}' }))
      let attention = {}
      try { attention = JSON.parse(att.stdout) } catch {}
      const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, ...agentExtractArgs(original, attention)],
        { timeout: 180_000 })
      const res = JSON.parse(stdout)
      return res.parsed ?? { ok: false, error: 'no block' }
    },
    async verifyAgent(item, original) {
      const { stdout } = await execFileP('node', [AGENT_RUN_SCRIPT, ...agentVerifyArgs(original, item)],
        { timeout: 180_000 })
      return JSON.parse(stdout).parsed ?? { ok: false }
    },
    async runDaemon(item) {
      const { stdout } = await execFileP(DAEMON_PY, [DAEMON_SCRIPT, ...daemonScheduleChangeArgs(item)],
        { timeout: 30_000 })
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

/** 命令链路:提取 → 校验 → daemon 写入 → 确认/追问/澄清记录(180s 超时不阻塞队列,M16) */
export async function handleScheduleCommand(original, msg, bot, deps) {
  const repoRoot = deps.repoRoot ?? REPO_ROOT
  try {
    const ex = await withTimeout(deps.extractAgent(original), 180_000)
    if (!ex.ok) {
      if (ex.not_command) return 'chat'                 // ⑤/⑥ 释放回聊天链(记录保留)
      const rec = { original, missing: ex.missing ?? [], question: ex.question ?? '哥哥没太听明白,再告诉哥哥一次具体安排?',
                    created_at: deps.now().toISOString(), expires_at: new Date(deps.now().getTime() + 6 * 3600e3).toISOString() }
      writeClarify(repoRoot, rec)
      await bot.reply(msg, rec.question)
      return 'clarify'
    }
    const vf = await withTimeout(deps.verifyAgent(ex.item, original), 180_000)
    if (!vf.ok) {
      const rec = { original, missing: vf.missing ?? [], question: vf.question ?? '这个安排有点对不上,哥哥再确认一下?',
                    created_at: deps.now().toISOString(), expires_at: new Date(deps.now().getTime() + 6 * 3600e3).toISOString() }
      writeClarify(repoRoot, rec)
      await bot.reply(msg, rec.question)
      return 'clarify'
    }
    const r = await withTimeout(deps.runDaemon(ex.item), 30_000)
    if (r.ok) {
      clearClarify(repoRoot)                            // ⑪ 成功写入后清记录(F4)
      await bot.reply(msg, r.text)
      return 'done'
    }
    // ⑩ 确定性拒绝(reason/question/missing 由 daemon 给出)→ 入澄清记录
    writeClarify(repoRoot, { original, missing: r.missing ?? [], question: r.question ?? '处理失败,再试一次?',
                             created_at: deps.now().toISOString(), expires_at: new Date(deps.now().getTime() + 6 * 3600e3).toISOString() })
    await bot.reply(msg, r.question)
    return 'rejected'
  } catch (err) {
    await bot.reply(msg, '⚠️ 处理失败,再试一次?').catch(() => {})   // 超时/异常兜底(M16/C7)
    return 'error'
  }
}

// ── 主会话每日轮换（每 check_minutes 整点检查,空闲超 idle_minutes 才轮换；见 session-rotate.mjs）──
// 活动判定:最近 1h 内有用户消息（onMessage 写 activity）或 cron 判定要发消息
// （chiguo-tick.sh ACTION=send 写 activity）→ 顺延到下一检查点；绝不切断进行中的对话。
// 启动即检查（bridge 重启/宕机错过 → 补轮换）；轮换经 TurnQueue 串行，不与在途 agent turn 交错。
export function armSessionRotation(queue) {
  if (!ROTATE_CFG.enabled) return
  const { backupsDir, markerPath, activityFile } = defaultRotatePaths()
  const activityPath = process.env.WECHAT_BRIDGE_ACTIVITY_FILE ?? activityFile
  const tick = async () => {
    try {
      const done = await queue.run(() => rotateIfDue({
        markerPath, backupsDir, cwd: BRIDGE_DIR,
        rpc: globalThis.__agentRpc ?? null,
        activityPath, idleMinutes: ROTATE_CFG.idleMinutes,
      }))
      if (done && typeof done === 'object') {
        console.log(`[rotate] 主会话已轮换（${cstDateStr()}）: main=${done.main ?? '无'}`)
      } else if (done === 'active') {
        console.log(`[rotate] 顺延: 近期有活动（${ROTATE_CFG.idleMinutes}min 空闲才轮换）`)
      }
    } catch (err) {
      console.error('[rotate] 失败:', err instanceof Error ? err.message : String(err))
    }
    setTimeout(tick, msToNextCheck(new Date(), ROTATE_CFG.checkMinutes))
  }
  tick()
}
