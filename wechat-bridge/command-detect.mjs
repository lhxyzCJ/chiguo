#!/usr/bin/env node
/**
 * command-detect — 特殊命令检测 + daemon 执行（Phase 4 Task 14 闭环）
 *
 * 背景：openclaw standing order 停用后，纪念日/假期（--anniversary/--break）指令链路
 * 一度断开（pi 为纯文本调用，无工具权限）。方案 A：bridge 规则化接管——
 * 收到消息先正则检测命令意图，命中则直接执行 daemon CLI 并回复确认，不经过 pi。
 * 确定性优先：检测保守（短消息 + 非问句），歧义消息放行给 pi 自然回复。
 *
 * 用法（供 bridge.mjs import）：
 *   detectSpecialCommand(text) → null | { action, daemon: [...argv], hint }
 *   executeSpecialCommand(execFileP, spec, daemonPy, daemonScript) → { ok, reply }
 *   buildReply(action, result) → string（daemon JSON → 迟菓风确认文案）
 */
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const CST_OFFSET_MS = 8 * 3600 * 1000
const MAX_LEN = 40

function cstNow() {
  return new Date(Date.now() + CST_OFFSET_MS)
}

/** 推测倒计时年份：今年该日期已过 → 明年（CST）。 */
export function inferYear(month, day) {
  const now = cstNow()
  let year = now.getUTCFullYear()
  if (Date.UTC(year, month - 1, day) < Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate())) {
    year += 1
  }
  return year
}

/**
 * 检测特殊命令。返回 { action, daemon, hint }；非命令返回 null。
 * daemon: 拼好的 daemon argv（如 ['--anniversary', 'add anniversary 05-11 迟菓生日']）。
 * hint: 无 daemon 输出可解析时的兜底确认文案。
 */
export function detectSpecialCommand(text) {
  if (typeof text !== 'string') return null
  const t = text.trim()
  if (!t || t.length > MAX_LEN) return null
  if (/[吗？?]$/.test(t) || /^(你|您)/.test(t)) return null  // 问句/对话式提问不拦截

  // 1) 纪念日：记住X月X日(是|为)?XX → add anniversary MM-DD <name>（哥哥/主人 前缀兼容）
  let m = t.match(/^(?:哥哥|主人)?记住\s*(\d{1,2})月(\d{1,2})日\s*(?:是|为)?\s*(.+)$/)
  if (m) {
    const mm = String(Number(m[1])).padStart(2, '0')
    const dd = String(Number(m[2])).padStart(2, '0')
    const name = m[3].replace(/[。！!～~，,、了]+$/, '').trim()
    if (name) {
      return {
        action: 'anniversary_added',
        daemon: ['--anniversary', `add anniversary ${mm}-${dd} ${name}`],
        hint: `记住了！${m[1]}月${m[2]}日——${name}。`,
      }
    }
  }

  // 2) 倒计时：YYYY年M月D日(是|为|要)?XX / M月D日要XX → add countdown YYYY-MM-DD <name>
  m = t.match(/^(\d{4})年(\d{1,2})月(\d{1,2})日\s*(?:是|为|要)?\s*(.+)$/)
  if (m) {
    const name = m[4].replace(/[。！!～~，,、]+$/, '').trim()
    if (name) {
      const date = `${m[1]}-${String(Number(m[2])).padStart(2, '0')}-${String(Number(m[3])).padStart(2, '0')}`
      return {
        action: 'countdown_added',
        daemon: ['--anniversary', `add countdown ${date} ${name}`],
        hint: `嗯嗯，${name}（${date}）——我算着日子呢。`,
      }
    }
  }
  m = t.match(/^(\d{1,2})月(\d{1,2})日\s*要\s*(.+)$/)
  if (m) {
    const name = m[3].replace(/[。！!～~，,、了]+$/, '').trim()
    if (name) {
      const year = inferYear(Number(m[1]), Number(m[2]))
      const date = `${year}-${String(Number(m[1])).padStart(2, '0')}-${String(Number(m[2])).padStart(2, '0')}`
      return {
        action: 'countdown_added',
        daemon: ['--anniversary', `add countdown ${date} ${name}`],
        hint: `嗯嗯，${name}（${date}）——我算着日子呢。`,
      }
    }
  }

  // 3) 列表：有哪些纪念日 / 纪念日列表等（两分支均 ^ 锚定）
  if (/^(?:(?:有)?哪些纪念日|纪念日(?:列表|有哪些|查|看看))/.test(t)) {
    return { action: 'anniversary_list', daemon: ['--anniversary', 'list'], hint: '让我看看都有什么日子……' }
  }

  // 4) 假期：放假了/放暑假了 → --break on；开学了 → --break off
  if (/^(?:我(?:们)?)?(?:放暑假|放假)(?:了|啦|噜)?[。!！]?$/.test(t)) {
    return { action: 'break_on', daemon: ['--break', 'on'], hint: '……知道了。放假了。那我可以多找你说话啦。' }
  }
  if (/^(?:我(?:们)?)?开学(?:了|啦)?[。!！]?$/.test(t)) {
    return { action: 'break_off', daemon: ['--break', 'off'], hint: '哦。开学了……行吧，课表要紧。' }
  }

  return null
}

/** daemon 输出 JSON → 迟菓风确认文案。 */
export function buildReply(action, result) {
  if (result.error || result.ok === false) {
    return `处理失败：${result.error ?? result.message ?? '未知错误'}`
  }
  switch (action) {
    case 'anniversary_added':
      return `记住了！${result.date}——${result.name}。……哼，才不会忘记。`
    case 'countdown_added':
      return `嗯嗯，${result.name}（${result.date}）——我算着日子呢。`
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
  const repo = dirname(daemonScript)
  let stdout
  try {
    stdout = await new Promise((resolve, reject) => {
      const c = spawnFn(daemonPy, [daemonScript, ...spec.daemon], {
        stdio: ['ignore', 'pipe', 'pipe'],
        timeout: 30_000,
        cwd: join(repo),
      })
      let out = ''
      let err = ''
      c.stdout.on('data', (d) => { out += d })
      c.stderr.on('data', (d) => { err += d })
      c.on('error', (e) => reject(e))
      c.on('close', (code) => {
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
    return { ok: false, reply: `${spec.hint}（daemon 输出异常）` }
  }
  return { ok: !(result.error || result.ok === false), reply: buildReply(spec.action, result) }
}
