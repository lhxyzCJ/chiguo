/** wechat-bridge/command-detect.mjs — 斜杠命令（白名单制,确定性执行,不经 pi）。
 * 会话文件工具：encodeSessionDir / backupSessionFile（/new 与每日轮换共用）。 */
import { readdirSync, mkdirSync, renameSync, statSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { homeDir } from './home-dir.mjs'
import { REPO } from './env.mjs'

const SLASH_HELP = [
  '/help — 命令列表',
  '/new — 清空当前对话上下文',
  '/status — 会话与用量状态',
].join('\n')

/** 斜杠命令检测:全部 / 开头消息都命中(未知命令 → unknown_slash,由执行侧拒绝)。 */
export function detectSlashCommand(text) {
  if (typeof text !== 'string') return null
  const t = text.trim()
  if (!t.startsWith('/')) return null
  const parts = t.split(/\s+/)
  const cmd = parts[0]
  const arg = parts.slice(1).join(' ').trim()
  switch (cmd) {
    case '/new': return { action: 'new_session', slash: true }
    case '/status': return { action: 'status', slash: true }
    case '/help': case '/帮助': return { action: 'help', slash: true }
    default: return { action: 'unknown_slash', slash: true, arg: cmd }
  }
}

/** pi 会话目录编码:--root-chiguo-wechat-bridge-- 同款(packageManager getDefaultSessionDirPath)。 */
export function encodeSessionDir(cwd) {
  return '--' + cwd.replace(/^\//, '').replaceAll('/', '-') + '--'
}

/** 备份并移走最近一个 <suffix> 会话文件(与 AGENTRUN_NEW_SESSION 共享逻辑)。返回备份路径或 null。
 *  suffix 默认 chiguo-main（回复链）。 */
export function backupSessionFile(cwd, backupsDir, suffix = 'chiguo-main') {
  const dir = join(homeDir(), '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
  let files = []
  try { files = readdirSync(dir).filter((f) => f.endsWith(`_${suffix}.jsonl`)) } catch {}
  if (!files.length) return null
  files.sort()
  const src = join(dir, files[files.length - 1])
  mkdirSync(backupsDir, { recursive: true })
  const ts = new Date().toISOString().replace(/[:.]/g, '-')
  const dst = join(backupsDir, `${ts}-${suffix}.jsonl`)
  renameSync(src, dst)
  return dst
}

function fmtTokens(n) {
  if (n == null) return '?'
  return n.toLocaleString('en-US')
}

/** 执行斜杠命令(纯 node 侧:文件操作),不经 pi。 */
export function executeSlashCommand(spec, cwd) {
  const backups = join(homeDir(), '.chiguo', 'session-backups')
  switch (spec.action) {
    case 'new_session': {
      try {
        const dst = backupSessionFile(cwd, backups)
        return { ok: true, reply: dst ? '好，清一下。之前的事我都还记着。' : '嗯？现在没有可清的对话呀。' }
      } catch (err) {
        return { ok: false, reply: `处理失败：${err instanceof Error ? err.message : String(err)}` }
      }
    }
    case 'status': {
      try {
        let tele = null
        try {
          const lines = readFileSync(join(REPO, 'logs', 'agent-run.log'), 'utf8').trim().split('\n')
          if (lines.length) tele = JSON.parse(lines[lines.length - 1])
        } catch (err) {
          console.error('[status] agent-run.log 末行 JSON 解析失败:',
            err instanceof Error ? err.message : String(err))
        }
        const usage = tele?.usage ?? {}
        const total = (usage.cacheRead ?? 0) + (usage.input ?? 0)
        let fileSize = 0
        try {
          const dir = join(homeDir(), '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
          const files = readdirSync(dir).filter((f) => f.endsWith('_chiguo-main.jsonl')).sort()
          if (files.length) fileSize = statSync(join(dir, files[files.length - 1])).size
        } catch {}
        const pct = total ? ((total / 1_000_000) * 100).toFixed(2) : '0'
        const dur = tele?.dur_ms != null ? `${(tele.dur_ms / 1000).toFixed(1)}s` : '?'
        return {
          ok: true,
          reply: `会话 ${fmtTokens(total)} tokens / 1M（${pct}%）| 文件 ${Math.round(fileSize / 1024)}KB | 上次耗时 ${dur} | 缓存命中 ${fmtTokens(usage.cacheRead ?? 0)}`,
        }
      } catch (err) {
        return { ok: false, reply: `处理失败：${err instanceof Error ? err.message : String(err)}` }
      }
    }
    case 'help':
      return { ok: true, reply: SLASH_HELP }
    case 'unknown_slash':
    default:
      return { ok: true, reply: '这是什么咒语啦？我可不会~（发 /help 看看我会的）' }
  }
}
