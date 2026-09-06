#!/usr/bin/env node
/** wechat-bridge/env.mjs — 模块级 env 快照（进程启动 import 时一次读取）。
 *  测试在动态 import 前设置 WECHAT_BRIDGE_* env；本模块无内部依赖，各模块 import 时保持先 env 后其他。 */
import { resolveRepo, RUNNER, HOST } from '../scripts/agent-run.mjs'
import { dirname, join } from 'node:path'

export const DEBOUNCE_MS = 4000
// U8c: AGENT_RUN_SCRIPT 默认随仓库 scripts/ 部署(portable)，可用 WECHAT_BRIDGE_AGENT_RUN 覆盖;
// 启动时见 checkAgentRunScript/main()——缺失或文件不存在时明确报错，替代 ask 期通用失败文案。
export const AGENT_RUN_SCRIPT = process.env.WECHAT_BRIDGE_AGENT_RUN
  ?? new URL('../scripts/agent-run.mjs', import.meta.url).pathname
// RPC 常驻(仿 OpenClaw gateway):env WECHAT_BRIDGE_AGENT_RPC=1 显式启用;失败自动回退 spawn。
// RPC 是 agent 二进制特有协议(--mode rpc)——runner=command(自定义 agent)时强制关闭。
export const AGENT_RPC_ENABLED = RUNNER === 'agent' && process.env.WECHAT_BRIDGE_AGENT_RPC === '1'
export const SEND_PORT = Number(process.env.WECHAT_BRIDGE_SEND_PORT ?? 18790)
// F-A17-003: bot.send 底层不可取消——withTimeout 超时只代表「未在时限内确认送达」，
export const SEND_TIMEOUT_MS = Number(process.env.WECHAT_BRIDGE_SEND_TIMEOUT_MS ?? 30_000)
// R10 (F-A17-004): 发送侧 /agent/prompt 总超时预算（排队 + restart + 处理）对齐 tick 125s:
// 总预算默认 110s（< 125s，留 curl 网络余量）；排队等待预算默认 30s（queue_busy 快速判败）。
export const SEND_PROMPT_TOTAL_MS = Number(process.env.WECHAT_BRIDGE_SEND_PROMPT_TOTAL_MS ?? 110_000)
export const SEND_PROMPT_QUEUE_WAIT_MS = Number(process.env.WECHAT_BRIDGE_SEND_PROMPT_QUEUE_WAIT_MS ?? 30_000)
// #191: 未设置共享 token 时 /send 与 /agent/prompt 零鉴权 → main() FATAL 拒绝启动（require，而非跳过校验）。
export const BRIDGE_TOKEN = process.env.WECHAT_BRIDGE_TOKEN
export const OWNER_ID = process.env.WECHAT_BRIDGE_OWNER ?? 'owner@im.wechat'
// F-SEC-03 (#316): 白名单模式 —— 仅白名单联系人可对话；缺省（两者皆空）= 仅 owner（安全默认）。
export const REJECT_TEXT = process.env.WECHAT_BRIDGE_WHITELIST_REJECT
  ?? '这是迟菓的私人助手，暂不对陌生人开放哦'
export function resolveWhitelist() {
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
// ── 主会话每日轮换配置（toml [host].session_rotate_*；env WECHAT_BRIDGE_ACTIVITY_FILE 可覆盖活动文件路径）──
// 非法/非正配置值回退默认（不取 Math.max 下限——负数不应导致 5 分钟高频检查）
export const rotNum = (v, d) => { const n = Number(v); return Number.isFinite(n) && n >= 5 ? n : d }
export const ROTATE_CFG = {
  enabled: HOST.session_rotate_enabled !== false,
  checkMinutes: rotNum(HOST.session_rotate_check_minutes, 60),
  idleMinutes: rotNum(HOST.session_rotate_idle_minutes, 60),
}
// 仓库根 = 本文件位置推导（可移植，随仓库克隆到任何路径）
export const REPO = resolveRepo(import.meta.url)
// bridge 运行目录（wechat-bridge.sh 启动 cwd）：不得依赖 process.cwd()。
export const BRIDGE_DIR = join(REPO, 'wechat-bridge')
export const DAEMON_PY = process.env.WECHAT_BRIDGE_DAEMON_PY ?? `${REPO}/.venv/bin/python`
export const DAEMON_SCRIPT = process.env.WECHAT_BRIDGE_DAEMON ?? `${REPO}/chiguo_daemon.py`
// schedule 运行时文件锚定 daemon 所在目录（跟随 WECHAT_BRIDGE_DAEMON 覆盖；测试隔离依赖），与 REPO 仅默认相等
export const REPO_ROOT = dirname(DAEMON_SCRIPT)
// agent 假死记账脚本（agent_health.py 状态机）；agent_health 解释器独立于 DAEMON_PY
export const AGENT_HEALTH_SCRIPT = process.env.WECHAT_BRIDGE_AGENT_HEALTH
  ?? new URL('../scripts/agent_health.py', import.meta.url).pathname
export const AGENT_HEALTH_PY = process.env.WECHAT_BRIDGE_AGENT_HEALTH_PY ?? `${REPO}/.venv/bin/python`
// 登录态目录：默认仓库内回退；wechat-bridge.sh 注入集中认证目录；可用 WECHAT_BRIDGE_STORAGE 覆盖
export const DEFAULT_STORAGE = new URL('./credentials/', import.meta.url).pathname

/** /send 来源校验:Host/Origin 必须本地回环(127.0.0.1/localhost/::1)，容忍端口后缀。 */
export function isLocalHost(host) {
  if (!host) return false
  const h = String(host).toLowerCase().replace(/:\d+$/, '')
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]'
}
export function isLocalOrigin(origin) {
  if (!origin) return true  // curl 等无 Origin 客户端:靠 Host + token 把关
  try {
    const u = new URL(origin)
    return u.protocol === 'http:' && isLocalHost(u.host)
  } catch { return false }
}
