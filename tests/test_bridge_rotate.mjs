// test_bridge_rotate.mjs — 主会话每日轮换逻辑测试（独立 runner）
// 用法: node test_bridge_rotate.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { mkdtempSync, rmSync, existsSync, mkdirSync, writeFileSync, readdirSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { encodeSessionDir, backupSessionFile } from '../wechat-bridge/command-detect.mjs'
import {
  cstDateStr, dayStartUTC, msToNextCheck,
  readActivity, writeActivity, isIdleSince,
  rotateIfDue, readLastRotate,
} from '../wechat-bridge/session-rotate.mjs'

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

// ── 日历/时刻（纯函数）──
t('rotate: cstDateStr 边界（UTC 20:30 → CST 次日 04:30）', () => {
  assert.strictEqual(cstDateStr(new Date('2025-08-14T20:30:00.000Z')), '2025-08-15')
  assert.strictEqual(cstDateStr(new Date('2025-08-14T15:59:59.000Z')), '2025-08-14')
})
t('rotate: dayStartUTC 今天 00:00 CST → UTC 昨日 16:00', () => {
  // CST 2025-08-14 18:00
  assert.strictEqual(new Date(dayStartUTC(new Date('2025-08-14T10:00:00.000Z'))).toISOString(), '2025-08-13T16:00:00.000Z')
  // CST 2025-08-14 00:00 整（UTC 08-13T16:00）
  assert.strictEqual(new Date(dayStartUTC(new Date('2025-08-13T16:00:00.000Z'))).toISOString(), '2025-08-13T16:00:00.000Z')
})
t('rotate: msToNextCheck 整点对齐网格', () => {
  // CST 23:30 → 下一个整点 = 次日 00:00 = 30min
  assert.strictEqual(msToNextCheck(new Date('2025-08-13T15:30:00.000Z'), 60), 1_800_000)
  // CST 00:30 → 01:00 = 30min
  assert.strictEqual(msToNextCheck(new Date('2025-08-13T16:30:00.000Z'), 60), 1_800_000)
  // 正到整点 → 下一个整点 = 60min
  assert.strictEqual(msToNextCheck(new Date('2025-08-13T16:00:00.000Z'), 60), 3_600_000)
  // 30min 网格：CST 00:15 → 00:30 = 15min
  assert.strictEqual(msToNextCheck(new Date('2025-08-13T16:15:00.000Z'), 30), 900_000)
  // 非法/过小 → 退避 60s
  assert.strictEqual(msToNextCheck(new Date(), 0), 60_000)
  assert.strictEqual(msToNextCheck(new Date(), 'abc'), 60_000)
})

// ── 活动时间戳 ──
t('rotate: writeActivity/readActivity 往返 + 损坏容错', () => {
  const dir = mkdtempSync(join(tmpdir(), 'rotate-activity-'))
  try {
    const p = join(dir, 'sub', 'activity')
    writeActivity(p, 1234567890)
    assert.strictEqual(readActivity(p), 1234567890)
    writeFileSync(p, 'not-a-number')
    assert.strictEqual(readActivity(p), null, '损坏 → null(视为空闲)')
    writeFileSync(p, '')
    assert.strictEqual(readActivity(p), null)
    assert.strictEqual(readActivity(join(dir, 'missing')), null, '缺失 → null')
  } finally {
    rmSync(dir, { recursive: true, force: true })
  }
})
t('rotate: isIdleSince 阈值判定', () => {
  const now = new Date('2025-08-14T10:00:00.000Z')
  const nowSec = Math.floor(now.getTime() / 1000)
  assert.strictEqual(isIdleSince(now, null, 60), true, '无活动 → 空闲')
  assert.strictEqual(isIdleSince(now, nowSec - 10 * 60, 60), false, '10min 前活动 → 不空闲')
  assert.strictEqual(isIdleSince(now, nowSec - 61 * 60, 60), true, '61min 前活动 → 空闲')
  assert.strictEqual(isIdleSince(now, nowSec - 60 * 60, 60), true, '恰 60min → 空闲(>=)')
})

// ── backupSessionFile suffix 参数（send 每轮全新复用）──
t('rotate: backupSessionFile suffix 只移对应后缀文件', () => {
  const home = mkdtempSync(join(tmpdir(), 'rotate-suffix-home-'))
  const prevHome = process.env.HOME
  process.env.HOME = home
  try {
    const cwd = mkdtempSync(join(tmpdir(), 'rotate-suffix-cwd-'))
    const dir = join(home, '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
    mkdirSync(dir, { recursive: true })
    const mainFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-main.jsonl')
    const sendFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-send.jsonl')
    writeFileSync(mainFile, 'x\n')
    writeFileSync(sendFile, 'y\n')
    const backups = join(home, '.chiguo', 'session-backups')
    const dst = backupSessionFile(cwd, backups, 'chiguo-send')
    assert.ok(dst.endsWith('-chiguo-send.jsonl'), `备份名带后缀: ${dst}`)
    assert.ok(!existsSync(sendFile), 'send 文件已移走')
    assert.ok(existsSync(mainFile), 'main 文件不受影响')
    assert.strictEqual(backupSessionFile(cwd, backups, 'chiguo-send'), null, '无 send 文件 → null')
    // 默认后缀行为不变（/new 路径）
    const dstMain = backupSessionFile(cwd, backups)
    assert.ok(dstMain.endsWith('-chiguo-main.jsonl'))
  } finally {
    process.env.HOME = prevHome
    rmSync(home, { recursive: true, force: true })
  }
})

// ── rotateIfDue 全流程（HOME 注入隔离，绝不触碰真实 ~/.pi、~/.chiguo）──
function rotateCtx(nowIso, { marker = null, activity = null } = {}) {
  const home = mkdtempSync(join(tmpdir(), 'rotate-home-'))
  const prevHome = process.env.HOME
  process.env.HOME = home
  const cwd = mkdtempSync(join(tmpdir(), 'rotate-cwd-'))
  const dir = join(home, '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
  mkdirSync(dir, { recursive: true })
  const mainFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-main.jsonl')
  const sendFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-send.jsonl')
  writeFileSync(mainFile, '{"type":"session"}\n')
  writeFileSync(sendFile, '{"type":"session"}\n')
  const backups = join(home, '.chiguo', 'session-backups')
  const markerPath = join(home, '.chiguo', 'session-rotate-last')
  const activityPath = join(home, '.chiguo', 'session-activity-last')
  mkdirSync(join(home, '.chiguo'), { recursive: true })
  if (marker) writeFileSync(markerPath, marker)
  if (activity != null) writeActivity(activityPath, activity)
  const restarts = { n: 0 }
  const rpc = { async restart() { restarts.n++ } }
  const cleanup = () => { process.env.HOME = prevHome; rmSync(home, { recursive: true, force: true }) }
  return { home, cwd, dir, mainFile, sendFile, backups, markerPath, activityPath, rpc, restarts, cleanup }
}

t('rotate: 近期有活动 → active 顺延,不轮换', async () => {
  const now = new Date('2025-08-14T10:00:00.000Z')  // CST 08-14 18:00
  const ctx = rotateCtx('2025-08-14T10:00:00.000Z', { activity: Math.floor(now.getTime() / 1000) - 10 * 60 })
  try {
    const out = await rotateIfDue({ now, markerPath: ctx.markerPath, backupsDir: ctx.backups, cwd: ctx.cwd, rpc: ctx.rpc, activityPath: ctx.activityPath, idleMinutes: 60 })
    assert.strictEqual(out, 'active')
    assert.ok(existsSync(ctx.mainFile), '会话文件未动')
    assert.ok(!existsSync(ctx.markerPath), '未写标记')
  } finally { ctx.cleanup() }
})
t('rotate: 空闲超阈值 → 轮换 main（只动 main,不碰 send）+ RPC 重启一次 + 写标记', async () => {
  const now = new Date('2025-08-14T10:00:00.000Z')  // CST 08-14 18:00
  const ctx = rotateCtx('2025-08-14T10:00:00.000Z', { activity: Math.floor(now.getTime() / 1000) - 70 * 60 })
  try {
    const out = await rotateIfDue({ now, markerPath: ctx.markerPath, backupsDir: ctx.backups, cwd: ctx.cwd, rpc: ctx.rpc, activityPath: ctx.activityPath, idleMinutes: 60 })
    assert.ok(out && out.main.endsWith('-chiguo-main.jsonl'), 'main 备份路径')
    assert.ok(!existsSync(ctx.mainFile), 'main 文件已移走')
    assert.ok(existsSync(ctx.sendFile), 'send 文件不受每日轮换影响（只动 main）')
    assert.ok(readdirSync(ctx.backups).some((f) => f.endsWith('-chiguo-main.jsonl')), 'main 备份存在')
    assert.ok(!readdirSync(ctx.backups).some((f) => f.endsWith('-chiguo-send.jsonl')), '无 send 备份（每日轮换只动 main）')
    assert.strictEqual(readLastRotate(ctx.markerPath), '2025-08-14', '幂等标记已写')
    assert.strictEqual(ctx.restarts.n, 1, 'RPC 重启一次（先杀进程再备份，#192）')
    // 同日再调 → 幂等跳过
    assert.strictEqual(await rotateIfDue({ now, markerPath: ctx.markerPath, backupsDir: ctx.backups, cwd: ctx.cwd, rpc: ctx.rpc, activityPath: ctx.activityPath, idleMinutes: 60 }), false)
    assert.strictEqual(ctx.restarts.n, 1, '幂等不重复重启')
  } finally { ctx.cleanup() }
})
t('rotate: 无 rpc（非 RPC 模式）也能轮换;无活动文件视为空闲', async () => {
  const now = new Date('2025-08-14T10:00:00.000Z')
  const ctx = rotateCtx('2025-08-14T10:00:00.000Z')
  try {
    const out = await rotateIfDue({ now, markerPath: ctx.markerPath, backupsDir: ctx.backups, cwd: ctx.cwd, rpc: null, activityPath: ctx.activityPath, idleMinutes: 60 })
    assert.ok(out && out.main.endsWith('-chiguo-main.jsonl'), '非 RPC 模式轮换')
    assert.strictEqual(readLastRotate(ctx.markerPath), '2025-08-14')
  } finally { ctx.cleanup() }
})
t('rotate: 无会话文件也写标记（新机不整天重试）', async () => {
  const now = new Date('2025-08-14T10:00:00.000Z')
  const ctx = rotateCtx('2025-08-14T10:00:00.000Z')
  try {
    rmSync(ctx.mainFile)
    rmSync(ctx.sendFile)
    const out = await rotateIfDue({ now, markerPath: ctx.markerPath, backupsDir: ctx.backups, cwd: ctx.cwd, rpc: ctx.rpc })
    assert.ok(out && out.main === null, '无文件也返回轮换结果')
    assert.strictEqual(readLastRotate(ctx.markerPath), '2025-08-14')
  } finally { ctx.cleanup() }
})
t('rotate: 当日已轮换标记 → 即使空闲也跳过', async () => {
  const now = new Date('2025-08-14T10:00:00.000Z')
  const ctx = rotateCtx('2025-08-14T10:00:00.000Z', { marker: '2025-08-14' })
  try {
    assert.strictEqual(await rotateIfDue({ now, markerPath: ctx.markerPath, backupsDir: ctx.backups, cwd: ctx.cwd, rpc: ctx.rpc }), false)
    assert.ok(existsSync(ctx.mainFile), '会话文件未动')
  } finally { ctx.cleanup() }
})

;(async () => {
  await runAll()
  console.log(`test_bridge_rotate: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1) })
