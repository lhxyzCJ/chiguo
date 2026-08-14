// test_bridge_rotate.mjs — 每日会话轮换逻辑测试（独立 runner）
// 用法: node test_bridge_rotate.mjs（退出码 0=全过，1=有失败）
import assert from 'node:assert'
import { mkdtempSync, rmSync, existsSync, mkdirSync, writeFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { encodeSessionDir, backupSessionFile } from '../wechat-bridge/command-detect.mjs'
import {
  parseRotateTime, cstDateStr, rotateInstantUTC, msToNextRotate,
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

// ── 时刻解析/日历（纯函数）──
t('rotate: parseRotateTime 合法/非法', () => {
  assert.deepStrictEqual(parseRotateTime('04:00'), { h: 4, m: 0 })
  assert.deepStrictEqual(parseRotateTime('4:30'), { h: 4, m: 30 })
  assert.deepStrictEqual(parseRotateTime('23:59'), { h: 23, m: 59 })
  assert.deepStrictEqual(parseRotateTime('00:00'), { h: 0, m: 0 })
  assert.strictEqual(parseRotateTime('24:00'), null)
  assert.strictEqual(parseRotateTime('04:60'), null)
  assert.strictEqual(parseRotateTime('04:0'), null)
  assert.strictEqual(parseRotateTime('abc'), null)
  assert.strictEqual(parseRotateTime(''), null)
  assert.strictEqual(parseRotateTime(null), null)
})
t('rotate: cstDateStr 边界（UTC 20:30 → CST 次日 04:30）', () => {
  assert.strictEqual(cstDateStr(new Date('2025-08-14T20:30:00.000Z')), '2025-08-15')
  assert.strictEqual(cstDateStr(new Date('2025-08-14T15:59:59.000Z')), '2025-08-14')
})
t('rotate: rotateInstantUTC 今天（CST）04:00 → UTC 昨日 20:00', () => {
  const now = new Date('2025-08-14T10:00:00.000Z')  // CST 08-14 18:00
  assert.strictEqual(new Date(rotateInstantUTC(now, '04:00')).toISOString(), '2025-08-13T20:00:00.000Z')
  assert.ok(Number.isNaN(rotateInstantUTC(now, 'abc')), '非法时刻 → NaN')
})
t('rotate: msToNextRotate 今天未到 → 今天；已过 → 明天；正到 → 明天', () => {
  // CST 08-14 02:00（未到 04:00）→ 2h
  assert.strictEqual(msToNextRotate(new Date('2025-08-13T18:00:00.000Z'), '04:00'), 7_200_000)
  // CST 08-14 18:00（已过 04:00）→ 明天 04:00 = 10h
  assert.strictEqual(msToNextRotate(new Date('2025-08-14T10:00:00.000Z'), '04:00'), 36_000_000)
  // 正到时刻 → 24h
  assert.strictEqual(msToNextRotate(new Date('2025-08-13T20:00:00.000Z'), '04:00'), 86_400_000)
  assert.ok(Number.isNaN(msToNextRotate(new Date(), 'abc')), '非法时刻 → NaN')
})

// ── backupSessionFile suffix 参数 ──
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
t('rotate: 到点且当日未轮换 → 双会话备份 + 写标记 + RPC 重启一次', async () => {
  const home = mkdtempSync(join(tmpdir(), 'rotate-home-'))
  const prevHome = process.env.HOME
  process.env.HOME = home
  try {
    const cwd = mkdtempSync(join(tmpdir(), 'rotate-cwd-'))
    const dir = join(home, '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
    mkdirSync(dir, { recursive: true })
    const mainFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-main.jsonl')
    const sendFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-send.jsonl')
    writeFileSync(mainFile, '{"type":"session"}\n')
    writeFileSync(sendFile, '{"type":"session"}\n')
    const backups = join(home, '.chiguo', 'session-backups')
    const marker = join(home, '.chiguo', 'session-rotate-last')
    const now = new Date('2025-08-14T10:00:00.000Z')  // CST 08-14 18:00，已过 04:00
    let restarts = 0
    const rpc = { async restart() { restarts++ } }
    const out = await rotateIfDue({ now, timeStr: '04:00', markerPath: marker, backupsDir: backups, cwd, rpc })
    assert.ok(out, '应执行轮换')
    assert.ok(out.main.endsWith('-chiguo-main.jsonl') && out.send.endsWith('-chiguo-send.jsonl'), '双备份路径')
    assert.ok(!existsSync(mainFile) && !existsSync(sendFile), '双会话文件已移走')
    assert.ok(readdirSync(backups).some((f) => f.endsWith('-chiguo-main.jsonl')), 'main 备份存在')
    assert.ok(readdirSync(backups).some((f) => f.endsWith('-chiguo-send.jsonl')), 'send 备份存在')
    assert.strictEqual(readLastRotate(marker), '2025-08-14', '幂等标记已写')
    assert.strictEqual(restarts, 1, 'RPC 重启一次（先杀进程再备份，#192）')
    // 同日再调 → 幂等跳过（不重复重启）
    assert.strictEqual(await rotateIfDue({ now, timeStr: '04:00', markerPath: marker, backupsDir: backups, cwd, rpc }), false)
    assert.strictEqual(restarts, 1, '幂等不重复重启')
  } finally {
    process.env.HOME = prevHome
    rmSync(home, { recursive: true, force: true })
  }
})
t('rotate: 未到轮换时刻 → 不轮换、不写标记', async () => {
  const home = mkdtempSync(join(tmpdir(), 'rotate-home-'))
  const prevHome = process.env.HOME
  process.env.HOME = home
  try {
    const cwd = mkdtempSync(join(tmpdir(), 'rotate-cwd-'))
    const dir = join(home, '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
    mkdirSync(dir, { recursive: true })
    const mainFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-main.jsonl')
    writeFileSync(mainFile, 'x\n')
    const backups = join(home, '.chiguo', 'session-backups')
    const marker = join(home, '.chiguo', 'session-rotate-last')
    const now = new Date('2025-08-13T18:00:00.000Z')  // CST 08-14 02:00 < 04:00
    const out = await rotateIfDue({ now, timeStr: '04:00', markerPath: marker, backupsDir: backups, cwd, rpc: null })
    assert.strictEqual(out, false)
    assert.ok(existsSync(mainFile), '会话文件未动')
    assert.ok(!existsSync(marker), '未写标记')
  } finally {
    process.env.HOME = prevHome
    rmSync(home, { recursive: true, force: true })
  }
})
t('rotate: 标记当日已轮换 → 即使到点也跳过；无 rpc（非 RPC 模式）也能轮换', async () => {
  const home = mkdtempSync(join(tmpdir(), 'rotate-home-'))
  const prevHome = process.env.HOME
  process.env.HOME = home
  try {
    const cwd = mkdtempSync(join(tmpdir(), 'rotate-cwd-'))
    const dir = join(home, '.pi', 'agent', 'sessions', encodeSessionDir(cwd))
    mkdirSync(dir, { recursive: true })
    const mainFile = join(dir, '2099-01-01T00-00-00-000Z_chiguo-main.jsonl')
    writeFileSync(mainFile, 'x\n')
    const backups = join(home, '.chiguo', 'session-backups')
    const marker = join(home, '.chiguo', 'session-rotate-last')
    mkdirSync(join(home, '.chiguo'), { recursive: true })
    writeFileSync(marker, '2025-08-14')
    const now = new Date('2025-08-14T10:00:00.000Z')  // 到点
    assert.strictEqual(await rotateIfDue({ now, timeStr: '04:00', markerPath: marker, backupsDir: backups, cwd, rpc: null }), false)
    assert.ok(existsSync(mainFile), '会话文件未动')
    // 清标记 → rpc=null（非 RPC 模式）到点正常轮换
    rmSync(marker)
    const out = await rotateIfDue({ now, timeStr: '04:00', markerPath: marker, backupsDir: backups, cwd, rpc: null })
    assert.ok(out && out.main.endsWith('-chiguo-main.jsonl') && out.send === null, '非 RPC 模式轮换（无 send 文件 → null）')
    assert.strictEqual(readLastRotate(marker), '2025-08-14')
  } finally {
    process.env.HOME = prevHome
    rmSync(home, { recursive: true, force: true })
  }
})
t('rotate: 无会话文件也写标记（新机不整天重试）', async () => {
  const home = mkdtempSync(join(tmpdir(), 'rotate-home-'))
  const prevHome = process.env.HOME
  process.env.HOME = home
  try {
    const cwd = mkdtempSync(join(tmpdir(), 'rotate-cwd-'))
    const backups = join(home, '.chiguo', 'session-backups')
    const marker = join(home, '.chiguo', 'session-rotate-last')
    const now = new Date('2025-08-14T10:00:00.000Z')
    const out = await rotateIfDue({ now, timeStr: '04:00', markerPath: marker, backupsDir: backups, cwd, rpc: null })
    assert.ok(out && out.main === null && out.send === null, '无文件也返回轮换结果')
    assert.strictEqual(readLastRotate(marker), '2025-08-14')
  } finally {
    process.env.HOME = prevHome
    rmSync(home, { recursive: true, force: true })
  }
})

;(async () => {
  await runAll()
  console.log(`test_bridge_rotate: ${passed}/${tests.length} passed`)
})().catch((e) => { console.error('FAIL', e); process.exit(1) })
