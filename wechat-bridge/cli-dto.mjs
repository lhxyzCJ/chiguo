/**
 * cli-dto — 子进程 argv 的 DTO 校验 + 组装（Issue #391）。
 *
 * 现状：全部子进程调用已是数组传参（无 shell 拼接），剩余真实缺口是
 * 用户/LLM 来源文本以 argv 元素原样传入、缺集中入参校验。本模块收敛
 * bridge.mjs 主链 + command-detect.mjs 特殊命令链路的全部 argv 组装：
 * 类型守卫 + 白名单，非法入参抛 TypeError/RangeError，调用方按既有
 * 失败路径处理（record/upgrade/health 吞错保回复流，其余走失败文案）。
 *
 * 约定：builder 只返回 flag 部分 argv（不含可执行文件/脚本路径），
 * 调用方自行前置模块级路径常量（DAEMON_SCRIPT / AGENT_RUN_SCRIPT 等）。
 */

/** --schedule-change item kind 白名单（与 cli/parser.py --schedule-change 帮助对齐：
 *  reminder/add/cancel/move/exam_week/remove）。 */
export const SCHEDULE_CHANGE_KINDS = ['reminder', 'add', 'cancel', 'move', 'exam_week', 'remove']

/** scripts/agent_health.py record --outcome choices 白名单。 */
export const HEALTH_OUTCOMES = ['fail', 'send_fail', 'success']

/** 特殊命令 payload kind 白名单（command-detect DTO）。 */
export const SPECIAL_KINDS = ['anniversary_add', 'anniversary_list', 'reminder', 'break_on', 'break_off', 'schedule_change']

const MMDD_RE = /^\d{2}-\d{2}$/
const FULL_DATE_RE = /^\d{4}-\d{2}-\d{2}$/
const DIM = [31, 29, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]  // 2 月按 29 容忍闰年

/** 非空字符串守卫。 */
export function assertText(value, name = 'text') {
  if (typeof value !== 'string' || !value) throw new TypeError(`${name} 必须是非空字符串`)
  return value
}

/** 用户可见短文本（名称/标签）：非空、无控制字符（防 \n 等破坏单元素 argv 语义）。 */
function assertLabel(value, name) {
  assertText(value, name)
  if (/[\0-\x1f\x7f]/.test(value)) throw new TypeError(`${name} 含非法控制字符`)
  return value
}

/** MM-DD 日期守卫（格式 + 月/日范围）。 */
function assertDateMMDD(date) {
  if (typeof date !== 'string' || !MMDD_RE.test(date)) {
    throw new TypeError(`date 非法（期望 MM-DD）：${String(date).slice(0, 20)}`)
  }
  const m = Number(date.slice(0, 2))
  const d = Number(date.slice(3, 5))
  if (!(m >= 1 && m <= 12 && d >= 1 && d <= DIM[m - 1])) throw new RangeError(`date 非法月份/日期：${date}`)
  return date
}

/** YYYY-MM-DD 日期守卫（格式 + 月/日范围；语义如 past_date 由 daemon 判定）。 */
function assertDateFull(date) {
  if (typeof date !== 'string' || !FULL_DATE_RE.test(date)) {
    throw new TypeError(`date 非法（期望 YYYY-MM-DD）：${String(date).slice(0, 20)}`)
  }
  const m = Number(date.slice(5, 7))
  const d = Number(date.slice(8, 10))
  if (!(m >= 1 && m <= 12 && d >= 1 && d <= DIM[m - 1])) throw new RangeError(`date 非法月份/日期：${date}`)
  return date
}

/** 对象守卫（接受对象或 JSON 字符串；拒绝 null/数组）。 */
export function assertJsonObject(value, name) {
  let obj = value
  if (typeof obj === 'string') {
    try { obj = JSON.parse(obj) } catch { throw new TypeError(`${name} 非法 JSON`) }
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) throw new TypeError(`${name} 必须是对象`)
  return obj
}

/** 白名单守卫。 */
function assertKind(value, kinds, name) {
  if (typeof value !== 'string' || !kinds.includes(value)) {
    throw new RangeError(`${name} 非法（白名单：${kinds.join('/')}）`)
  }
  return value
}

// ── agent-run argv ──

/** agent 分析调用：--prompt 原文 --analysis-mode。 */
export function agentAnalysisArgs(text) {
  return ['--prompt', assertText(text, 'prompt'), '--analysis-mode']
}

/** agent 第二趟（recall）：事实只走 --facts（JSON 数组），prompt 只放用户问题。 */
export function agentRecallArgs(prompt, facts) {
  assertText(prompt, 'prompt')
  let parsed = facts
  if (typeof parsed === 'string') {
    try { parsed = JSON.parse(parsed) } catch { throw new TypeError('facts 非法 JSON') }
  }
  if (!Array.isArray(parsed)) throw new TypeError('facts 必须是数组')
  return ['--prompt', prompt, '--schedule-recall', '--facts', JSON.stringify(parsed)]
}

/** 命令链路提取：--schedule-extract + --attention 快照 + --week-num（非数值回退 1）。 */
export function agentExtractArgs(original, attention) {
  assertText(original, 'original')
  const att = (attention && typeof attention === 'object' && !Array.isArray(attention)) ? attention : {}
  const week = Number(att.week_num ?? 1)
  return ['--prompt', original, '--schedule-extract', '--attention', JSON.stringify(att),
    '--week-num', Number.isFinite(week) ? String(Math.trunc(week)) : '1']
}

/** 命令链路校验：--schedule-verify + --item（对象）。 */
export function agentVerifyArgs(original, item) {
  assertText(original, 'original')
  return ['--prompt', original, '--schedule-verify', '--item', JSON.stringify(assertJsonObject(item, 'item'))]
}

// ── daemon argv ──

/** 回复侧记忆检索：--memory-search 查询串。 */
export function daemonMemorySearchArgs(query) {
  return ['--memory-search', assertText(query, 'query')]
}

/** recall 信号路由：--schedule-recall 查询串（LLM 产出，必须是干净字符串）。 */
export function daemonRecallArgs(recall) {
  return ['--schedule-recall', assertText(recall, 'recall')]
}

/** 确定性记录主人消息：--user-msg 原文（+ 可选 --recv-id 去重 id）。 */
export function daemonUserMsgArgs(text, recvId) {
  assertText(text, 'user-msg')
  const args = ['--user-msg', text]
  if (recvId) args.push('--recv-id', assertText(recvId, 'recv-id'))
  return args
}

/** 分析升级：--user-msg 原文 --analysis JSON（对象或 JSON 字符串；+ 可选 --recv-id）。 */
export function daemonAnalysisArgs(text, analysis, recvId) {
  assertText(text, 'user-msg')
  const args = ['--user-msg', text, '--analysis', JSON.stringify(assertJsonObject(analysis, 'analysis'))]
  if (recvId) args.push('--recv-id', assertText(recvId, 'recv-id'))
  return args
}

/** 写安排：--schedule-change JSON（item.kind 白名单前置拒绝，畸形到不了 daemon）。 */
export function daemonScheduleChangeArgs(item) {
  const obj = assertJsonObject(item, 'item')
  assertKind(obj.kind, SCHEDULE_CHANGE_KINDS, 'item.kind')
  return ['--schedule-change', JSON.stringify(obj)]
}

// ── agent_health argv ──

/** 假死记账：record --outcome 白名单（+ 可选 --reason，截断 100）。 */
export function healthRecordArgs(outcome, reason) {
  const args = ['record', '--outcome', assertKind(outcome, HEALTH_OUTCOMES, 'outcome')]
  if (reason) args.push('--reason', String(reason).slice(0, 100))
  return args
}

// ── 特殊命令 payload DTO（command-detect 消费）──

/** payload 结构校验 + 归一化：非法抛错（调用方转 ok:false，不直拼 argv）。 */
export function assertSpecialPayload(payload) {
  const p = assertJsonObject(payload, 'payload')
  assertKind(p.kind, SPECIAL_KINDS, 'payload.kind')
  switch (p.kind) {
    case 'anniversary_add':
      return { kind: p.kind, date: assertDateMMDD(p.date), name: assertLabel(p.name, 'payload.name') }
    case 'anniversary_list':
      return { kind: p.kind }
    case 'reminder':
      return { kind: p.kind, date: assertDateFull(p.date), label: assertLabel(p.label, 'payload.label') }
    case 'break_on':
    case 'break_off':
      return { kind: p.kind }
    case 'schedule_change': {
      const item = assertJsonObject(p.item, 'payload.item')
      assertKind(item.kind, SCHEDULE_CHANGE_KINDS, 'payload.item.kind')
      return { kind: p.kind, item }
    }
  }
}

/** 特殊命令 DTO → daemon argv（唯一组装点；description 链路的双轨 daemon 字段已删）。 */
export function specialCommandArgs(payload) {
  const p = assertSpecialPayload(payload)
  switch (p.kind) {
    case 'anniversary_add': return ['--anniversary', `add anniversary ${p.date} ${p.name}`]
    case 'anniversary_list': return ['--anniversary', 'list']
    case 'reminder':
      return ['--schedule-change', JSON.stringify({ kind: 'reminder', when: { date: p.date }, label: p.label })]
    case 'break_on': return ['--break', 'on']
    case 'break_off': return ['--break', 'off']
    case 'schedule_change': return ['--schedule-change', JSON.stringify(p.item)]
  }
}
