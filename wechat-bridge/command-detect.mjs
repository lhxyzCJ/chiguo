#!/usr/bin/env node
/**
 * command-detect — 特殊命令检测 + daemon 执行（Phase 4 Task 14 闭环）
 *
 * 背景：pi 为纯文本调用无工具权限，纪念日/假期（--anniversary/--break）指令链路
 * 由 bridge 确定性接管。方案 A：bridge 规则化接管——
 * 收到消息先正则检测命令意图，命中则直接执行 daemon CLI 并回复确认，不经过 pi。
 * 确定性优先：检测保守（短消息 + 非问句），歧义消息放行给 agent 自然回复。
 *
 * 用法（供 bridge.mjs import）：
 *   detectSpecialCommand(text) → null | { action, payload: {...}, hint }（payload 是唯一 DTO，
 *     由 cli-dto.mjs 白名单/类型守卫校验后组装 argv，无双轨）
 *   executeSpecialCommand(execFileP, spec, daemonPy, daemonScript) → { ok, reply }
 *   buildReply(action, result) → string（daemon JSON → 迟菓风确认文案）
 */
import { dirname, join } from 'node:path'
import { mkdirSync, readdirSync, renameSync, statSync, readFileSync } from 'node:fs'
import { homeDir } from './home-dir.mjs'
import { assertText, specialCommandArgs } from './cli-dto.mjs'

const CST_OFFSET_MS = 8 * 3600 * 1000
const MAX_LEN = 40

function cstNow() {
  return new Date(Date.now() + CST_OFFSET_MS)
}

/** 推测一次性提醒年份：今年该日期已过 → 明年（CST）。 */
export function inferYear(month, day) {
  const now = cstNow()
  let year = now.getUTCFullYear()
  if (Date.UTC(year, month - 1, day) < Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) {
    year += 1
  }
  return year
}

const DAYS_IN_MONTH = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]

/** 月/日范围校验（2 月按 29 容忍闰年）：非法日期（13月/2月30日）→ false，调用方放行 agent。 */
function isValidMonthDay(month, day) {
  return month >= 1 && month <= 12 && day >= 1 && day <= DAYS_IN_MONTH[month - 1]
}

/**
 * 检测特殊命令。返回 { action, payload, hint }；非命令返回 null。
 * payload: 结构化 DTO（见 cli-dto.mjs SPECIAL_KINDS），argv 由 executeSpecialCommand
 * 经 specialCommandArgs 校验组装，不在此处拼串。
 * hint: 无 daemon 输出可解析时的兜底确认文案。
 */
export function detectSpecialCommand(text) {
  if (typeof text !== 'string') return null
  const t = text.trim()
  if (!t || t.length > MAX_LEN) return null
  if (/[吗？?]$/.test(t) || /^(你|您)/.test(t)) return null  // 问句/对话式提问不拦截

  // 1) 纪念日：记住X月X日(是|为)?XX → add anniversary MM-DD <name>（哥哥/主人 前缀兼容）
  let m = t.match(/^(?:哥哥|主人)?记住\s*(\d{1,2})月(\d{1,2})日\s*(?:是|为)?\s*(.+)$/)
  if (m && isValidMonthDay(Number(m[1]), Number(m[2]))) {
    const mm = String(Number(m[1])).padStart(2, '0')
    const dd = String(Number(m[2])).padStart(2, '0')
    const name = m[3].replace(/[。！!～~，,、了]+$/, '').trim()
    if (name && !isAskOrNegate(name)) {
      return {
        action: 'anniversary_added',
        payload: { kind: 'anniversary_add', date: `${mm}-${dd}`, name },
        hint: `记住了！${m[1]}月${m[2]}日——${name}。`,
      }
    }
  }

  // 2) 一次性提醒(6c:countdown 废弃 → 写 reminder,经 --schedule-change;显式日期直转写,确定性链路)
  m = t.match(/^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(?:是|为|要)?\s*(.+)$/)
  if (m && isValidMonthDay(Number(m[2]), Number(m[3]))) {
    const name = m[4].replace(/[。！!～~，,、]+$/, '').trim()
    if (name && !isAskOrNegate(name)) {
      const date = `${m[1]}-${String(Number(m[2])).padStart(2, '0')}-${String(Number(m[3])).padStart(2, '0')}`
      return {
        action: 'reminder_added',
        payload: { kind: 'reminder', date, label: name },
        hint: `嗯嗯，${name}（${date}）——我算着日子呢。`,
      }
    }
  }
  m = t.match(/^(\d{1,2})月(\d{1,2})日\s*要\s*(.+)$/)
  if (m && isValidMonthDay(Number(m[1]), Number(m[2]))) {
    const name = m[3].replace(/[。！!～~，,、了]+$/, '').trim()
    if (name && !isAskOrNegate(name)) {
      const year = inferYear(Number(m[1]), Number(m[2]))
      const date = `${year}-${String(Number(m[1])).padStart(2, '0')}-${String(Number(m[2])).padStart(2, '0')}`
      return {
        action: 'reminder_added',
        payload: { kind: 'reminder', date, label: name },
        hint: `嗯嗯，${name}（${date}）——我算着日子呢。`,
      }
    }
  }

  // 3) 列表：有哪些纪念日 / 纪念日列表等（两分支均 ^ 锚定）
  if (/^(?:(?:有)?哪些纪念日|纪念日(?:列表|有哪些|查|看看))/.test(t)) {
    return { action: 'anniversary_list', payload: { kind: 'anniversary_list' }, hint: '让我看看都有什么日子……' }
  }

  // 4) 假期：放假了/放暑假了 → --break on；开学了 → --break off
  if (/^(?:我(?:们)?)?(?:放暑假|放假)(?:了|啦|噜)?[。!！]?$/.test(t)) {
    return { action: 'break_on', payload: { kind: 'break_on' }, hint: '……知道了。放假了。那我可以多找你说话啦。' }
  }
  if (/^(?:我(?:们)?)?开学(?:了|啦)?[。!！]?$/.test(t)) {
    return { action: 'break_off', payload: { kind: 'break_off' }, hint: '哦。开学了……行吧，课表要紧。' }
  }

  return null
}

/** 语义反转守卫(Issue #130)：日期前缀正则捕获的剩余文本呈疑问/否定/征求语气 → 放行 agent 自然回复。
 *  征求/疑问词(要不要|是不是|可不可以|可以吗|好不好|行不行|能不能|行吗|能吗|吗|怎么|什么|怎么办|几点|何时)
 *  或以否定词开头(不|别|没)——如"2026年8月11日要不要一起吃饭"残留"不要一起吃饭"语义反转，一律不拦截。
 *  正例(要考试/过生日/迟菓生日)不含上述词，不受影响。 */
function isAskOrNegate(s) {
  return /要不要|是不是|可不可以|可以吗|好不好|行不行|能不能|行吗|能吗|吗|怎么|什么|怎么办|几点|何时/.test(s)
    || /^(?:不|别|没)/.test(s)
}

/** 写/回忆意图检测(schedule-center 6a):词表子串命中 + start-anchored 豁免 MAX_LEN(R1/MED 钉死)。
 * 误命中由 extract not_command 释放回聊天兜底(代码核验)。 */
const SCHEDULE_WORDS = [
  ['remove',    /取消|撤销/],
  ['move',      /调课|改到|调到/],
  ['add',       /加课|补课/],
  ['cancel',    /停课|不上课/],
  ['exam_week', /考试周/],
  // C1: 移除单字「要」(日常消息高频误命中);改为含日期/时间令牌的强模式——「X月X日」或提醒类词才命中
  ['reminder',  /记住|记得|提醒|\d{1,2}月\d{1,2}[日号]/],
]
const ANCHORED = /^(?:我(?:们)?)?(?:停课|不上课|调课|改到|调到|加课|补课|取消|撤销)/
const DATE_TOKEN = /\d{1,2}月\d{1,2}[日号]/
export function detectScheduleIntent(text) {
  if (typeof text !== 'string') return null
  const t = text.trim()
  if (!t || /[吗？?]$/.test(t) || /^(你|您)/.test(t)) return null
  if (t.length > MAX_LEN && !ANCHORED.test(t)) return null      // MAX_LEN 豁免:start-anchored
  for (const [intent, re] of SCHEDULE_WORDS) {
    if (re.test(t)) return { intent }                           // 词表子串命中(remove 先判)
  }
  if (DATE_TOKEN.test(t)) return { intent: 'extract' }          // 日期令牌兜底(MED/用户裁决)
  // C1: 无条件短消息兜底已移除——日常短消息(我要吃饭/晚安/哈哈)不再双倍 LLM 调用;
  // 短消息仅当含日期令牌才兜底(上方 DATE_TOKEN 已覆盖),extract not_command 仍可释放回聊天链
  return null
}

/** daemon 输出 JSON → 迟菓风确认文案。 */
export function buildReply(action, result) {
  if (action === 'reminder_added') {
    if (result.ok === false || result.error) {
      return `处理失败：${result.question ?? result.error ?? '未知错误'}`
    }
    return result.text ?? `记住了，${result.item?.label ?? ''}。`
  }
  if (result.error || result.ok === false) {
    return `处理失败：${result.error ?? result.message ?? '未知错误'}`
  }
  switch (action) {
    case 'anniversary_added':
      return `记住了！${result.date}——${result.name}。……哼，才不会忘记。`
    case 'anniversary_list': {
      const items = result.anniversaries ?? []
      if (!items.length) return '纪念日一个都没有……哼，那我先自己记住。'
      const lines = items.map((a) => `· ${a.name}（${a.date}${a.type === 'countdown' ? ' · 倒计时' : ''}）`)
      return `有 ${items.length} 个：\n${lines.join('\n')}`
    }
    case 'break_on':
      return '……知道了。放假了。那我可以多找你说话啦。'
    case 'break_off':
      return '哦。开学了……行吧，课表要紧。'
    default:
      return '嗯，好了。'
  }
}

/**
 * 执行特殊命令（daemon CLI），返回 { ok, reply }。
 * 用 spawn 收集 stdout：daemon 出错时输出错误 JSON 并以非零退出（如 anniversary 未知子命令），
 * stdout 必须保留以取出 error 文案（execFile 会在非零退出时丢 stdout）。
 * cwd 锚定 daemon 脚本目录（anniversary/break 状态文件随仓库根，防 cwd 写散）。
 */

export async function executeSpecialCommand(spawnFn, spec, daemonPy, daemonScript) {
  // #391: payload 是唯一 DTO，经 cli-dto 白名单/类型守卫校验后组装 argv；
  // 非法 payload → ok:false（不直拼、不抛未捕获异常）。
  let argv
  try {
    if (!spec || typeof spec !== 'object') throw new TypeError('spec 必须是对象')
    assertText(spec.action, 'spec.action')
    argv = specialCommandArgs(spec.payload)
  } catch (err) {
    return { ok: false, reply: `处理失败：${err instanceof Error ? err.message : String(err)}` }
  }
  const hint = typeof spec.hint === 'string' ? spec.hint : ''
  const repo = dirname(daemonScript)
  let stdout
  try {
    stdout = await new Promise((resolve, reject) => {
      const c = spawnFn(daemonPy, [daemonScript, ...argv], {
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 30_000,
        cwd: join(repo),
      })
      // B4: setEncoding 让 data 回调收到 string（StringDecoder 跨 chunk 拼接多字节中文，
      // 逐 chunk Buffer 累加会把切在多字节中间的字符解码成 U+FFFD——daemon JSON/确认文案均含中文）
      c.stdout.setEncoding('utf8')
      c.stderr.setEncoding('utf8')
      let out = ''
      let outBytes = 0
      let err = ''
      let killed = false
      const bail = (e) => { if (!killed) { killed = true; try { c.kill('SIGKILL') } catch {} reject(e) } }
      // U5 (#233, M-7): 套用 R19 上限——累计 stdout 超 16MB 即 kill+reject（防无界累积内存）；
      // stderr 保留尾部 256KB；B4 setEncoding 保留（中文跨 chunk）
      c.stdout.on('data', (d) => {
        out += d
        outBytes += Buffer.byteLength(d, 'utf8')
        if (outBytes > 16 * 1024 * 1024) {
          bail(new Error('daemon stdout 超过 16MB 上限（输出无界，已终止）'))
        }
      })
      c.stderr.on('data', (d) => {
        err += d
        if (Buffer.byteLength(err, 'utf8') > 1024 * 1024) err = err.slice(-256 * 1024)
      })
      c.on('error', (e) => { if (!killed) reject(e) })
      c.on('close', (code) => { if (killed) return
        if (code !== 0 && !out.trim()) {
          const reason = err.trim().slice(0, 200)
          reject(new Error(`daemon exited ${code}${reason ? `: ${reason}` : ''}`))
          return
        }
        resolve(out)
      })
    })
  } catch (err) {
    return { ok: false, reply: `处理失败：${err instanceof Error ? err.message : String(err)}` }
  }
  let result
  try {
    result = JSON.parse(stdout)
  } catch {
    return { ok: false, reply: `${hint}（daemon 输出异常）` }
  }
  return { ok: !(result.error || result.ok === false), reply: buildReply(spec.action, result) }
}

// ─────────────────────────────────────────────────────────────
// 微信端斜杠命令（白名单制,确定性执行,不经 pi;其余 / 开头一律迟菓风拒绝）
// ─────────────────────────────────────────────────────────────

const SLASH_HELP = [
  '/help — 命令列表',
  '/new — 清空当前对话上下文（记忆保留,之前的事我还都记着）',
  '/status — 上下文/缓存/记忆状态',
  '/记忆 — 记忆库统计',
  '/记得什么 <词> — 搜索记忆',
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
    case '/记忆': case '/memory': return { action: 'memory_stats', slash: true }
    case '/记得什么': case '/remember': return { action: 'memory_search', slash: true, arg }
    case '/help': case '/帮助': return { action: 'help', slash: true }
    default: return { action: 'unknown_slash', slash: true, arg: cmd }
  }
}

/** pi 会话目录编码:--root-chiguo-wechat-bridge-- 同款(packageManager getDefaultSessionDirPath)。 */
export function encodeSessionDir(cwd) {
  return '--' + cwd.replace(/^\//, '').replaceAll('/', '-') + '--'
}

/** 备份并移走最近一个 <suffix> 会话文件(与 AGENTRUN_NEW_SESSION 共享逻辑)。返回备份路径或 null。
 *  suffix 默认 chiguo-main（回复链）；chiguo-send（主动发送链）由每日轮换调用。 */
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

function runCli(spawnFn, args, timeoutMs = 30_000) {
  return runCmd(spawnFn, process.execPath, args, timeoutMs)
}

/**
 * 任意命令 spawn（v1.8/Q9：记忆斜杠命令走 memory 包 CLI(mem0 后端)）。
 * cwd: 可选，指定子进程工作目录；不传则继承父进程。记忆 CLI 必须锚定仓库根
 *      （python -m memory 依赖 CWD=repo 才能 import memory 包；生产 bridge 由
 *      wechat-bridge.sh 以 (cd "$BRIDGE_DIR" ...) 启动，父进程 CWD=wechat-bridge/）。
 */
function runCmd(spawnFn, cmd, args, timeoutMs = 30_000, cwd) {
  const opts = { stdio: ['ignore', 'pipe', 'pipe'], timeout: timeoutMs }
  if (cwd !== undefined) opts.cwd = cwd
  return new Promise((resolve, reject) => {
    const c = spawnFn(cmd, args, opts)
    // B4: setEncoding 同上——记忆 CLI 输出含中文（搜索/统计结果），跨 chunk 需 StringDecoder
    c.stdout.setEncoding('utf8')
    c.stderr.setEncoding('utf8')
    let out = ''
    let outBytes = 0
    let err = ''
    let killed = false
    const bail = (e) => { if (!killed) { killed = true; try { c.kill('SIGKILL') } catch {} reject(e) } }
    // U5 (#233, M-7): 套用 R19 上限——累计 stdout 超 16MB 即 kill+reject（防无界累积内存）；stderr 保留尾部 256KB；B4 setEncoding 保留
    c.stdout.on('data', (d) => {
      out += d
      outBytes += Buffer.byteLength(d, 'utf8')
      if (outBytes > 16 * 1024 * 1024) {
        bail(new Error(cmd + ' stdout 超过 16MB 上限（输出无界，已终止）'))
      }
    })
    c.stderr.on('data', (d) => {
      err += d
      if (Buffer.byteLength(err, 'utf8') > 1024 * 1024) err = err.slice(-256 * 1024)
    })
    c.on('error', (e) => { if (!killed) reject(e) })
    c.on('close', (code) => {
      if (killed) return
      if (code !== 0 && !out.trim()) reject(new Error(`exit ${code}: ${err.trim().slice(0, 120)}`))
      else resolve(out)
    })
  })
}

function fmtTokens(n) {
  if (n == null) return '?'
  return n.toLocaleString('en-US')
}

/** 执行斜杠命令(纯 node 侧:文件操作 + 记忆后端 CLI),不经 pi/daemon。
 *  Q9: 记忆命令直接走 memory 包 CLI(python -m memory, mem0 唯一记忆后端),
 *  /记忆、/记得什么 均可用。 */
export async function executeSlashCommand(spawnFn, spec, cwd) {
  const repo = process.env.CHIGUO_REPO ?? dirname(cwd)
  const backups = join(homeDir(), '.chiguo', 'session-backups')
  // 记忆后端 CLI：解释器/argv 均可 env 覆盖（测试注入 fake；生产默认 .venv python + -m memory）
  const memPy = process.env.WECHAT_BRIDGE_MEMORY_PY ?? join(repo, '.venv', 'bin', 'python')
  // argv 默认用数组（['-m','memory'] 承载含空格模块路径），env 覆盖仍走 split 空格——
  // 覆盖值须为无空格的单段 argv（如 fake 单文件路径），含空格路径请改注入完整 argv。
  const memCli = process.env.WECHAT_BRIDGE_MEMORY_CLI === undefined
    ? ['-m', 'memory']
    : process.env.WECHAT_BRIDGE_MEMORY_CLI.split(' ')
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
          const lines = readFileSync(join(repo, 'logs', 'agent-run.log'), 'utf8').trim().split('\n')
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
        let memCount = '?'
        try {
          const out = await runCmd(spawnFn, memPy, [...memCli, '--stats'], 30_000, repo)
          const m = out.match(/"total_memories":\s*(\d+)/)
          if (m) memCount = m[1]
        } catch {}
        const pct = total ? ((total / 1_000_000) * 100).toFixed(2) : '0'
        const dur = tele?.dur_ms != null ? `${(tele.dur_ms / 1000).toFixed(1)}s` : '?'
        return {
          ok: true,
          reply: `会话 ${fmtTokens(total)} tokens / 1M（${pct}%）| 文件 ${Math.round(fileSize / 1024)}KB | 上次耗时 ${dur} | 缓存命中 ${fmtTokens(usage.cacheRead ?? 0)} | 记忆 ${memCount} 条`,
        }
      } catch (err) {
        return { ok: false, reply: `处理失败：${err instanceof Error ? err.message : String(err)}` }
      }
    }
    case 'memory_stats': {
      try {
        const out = await runCmd(spawnFn, memPy, [...memCli, '--stats'], 30_000, repo)
        let n = '?'
        try { n = JSON.parse(out).total_memories ?? '?' } catch {}
        return { ok: true, reply: `记忆库共 ${n} 条。哼，重要的事我都记着呢。` }
      } catch (err) {
        return { ok: false, reply: `记忆库打盹了：${err instanceof Error ? err.message : String(err)}` }
      }
    }
    case 'memory_search': {
      if (!spec.arg) return { ok: true, reply: '想查什么？给我个词呀——比如说「记得什么 火锅」。' }
      try {
        const out = await runCmd(spawnFn, memPy, [...memCli, '--search', spec.arg], 30_000, repo)
        const lines = out.split('\n').map((s) => s.trim()).filter(Boolean).slice(0, 3)
        if (!lines.length) return { ok: true, reply: `……「${spec.arg}」？没印象。哼，记性不好的是你吧。` }
        const items = lines.map((l) => l.replace(/^\[[^\]]*\]\s*/, '').slice(0, 60))
        return { ok: true, reply: `记得的：\n${items.map((s) => `· ${s}`).join('\n')}` }
      } catch (err) {
        return { ok: false, reply: `记忆库打盹了：${err instanceof Error ? err.message : String(err)}` }
      }
    }
    case 'help':
      return { ok: true, reply: SLASH_HELP }
    case 'unknown_slash':
    default:
      return { ok: true, reply: '这是什么咒语啦？我可不会~（发 /help 看看我会的）' }
  }
}
