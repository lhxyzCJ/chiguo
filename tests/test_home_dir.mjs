// test_home_dir.mjs — homeDir() 统一 home 解析回归（#357 Bun home 解析统一）
// 用法: node test_home_dir.mjs（退出码 0=全过，1=有失败）
// 背景：本机 node 为 Bun wrapper，os.homedir() 启动时捕获/缓存 passwd home（恒返回 /root），
// 不跟随运行期 process.env.HOME 改动；而生产代码用 os.homedir() 定位 ~/.pi、~/.chiguo，
// 测试注入临时 HOME 时去找不到 → 备份/轮换断言失败。homeDir() = HOME || os.homedir() 统一修复。
import assert from 'node:assert'
import os from 'node:os'
import { homeDir } from '../wechat-bridge/home-dir.mjs'

let passed = 0
const tests = []
function t(name, fn) { tests.push({ name, fn }) }
async function runAll() {
  for (const { name, fn } of tests) {
    try { await fn(); passed++; console.log(`  ok - ${name}`) }
    catch (e) { console.error(`  FAIL - ${name}`); throw e }
  }
}

t('homeDir 默认约等于 os.homedir()（生产 $HOME==passwd home 行为等价）', () => {
  // 前提基线：os.homedir() 自身存在且非空（两种 runtime 下都应返回 home）
  const fallback = os.homedir()
  assert.ok(fallback && typeof fallback === 'string', `os.homedir() 异常: ${fallback}`)
  const prevHome = process.env.HOME
  try {
    if (prevHome === undefined) delete process.env.HOME
    else process.env.HOME = prevHome
    assert.strictEqual(homeDir(), fallback)
  } finally {
    if (prevHome === undefined) delete process.env.HOME
    else process.env.HOME = prevHome
  }
})

t('homeDir 优先运行期 $HOME（#357 核心回归：与 os.homedir() 解耦）', () => {
  const prevHome = process.env.HOME
  try {
    // 注入非真实 home：Bun 下 os.homedir() 仍返回 /root（passwd 缓存），
    // homeDir() 必须跟随 $HOME 才能让测试临时目录生效（官方 node 此处同语义）。
    process.env.HOME = '/tmp/chiguo-home-dir-test'
    assert.strictEqual(homeDir(), '/tmp/chiguo-home-dir-test')
  } finally {
    if (prevHome === undefined) delete process.env.HOME
    else process.env.HOME = prevHome
  }
})

t('homeDir 空串 $HOME → 回退 os.homedir()（|| 语义，不算有效 home）', () => {
  const prevHome = process.env.HOME
  try {
    process.env.HOME = ''
    assert.strictEqual(homeDir(), os.homedir())
  } finally {
    if (prevHome === undefined) delete process.env.HOME
    else process.env.HOME = prevHome
  }
})

t('homeDir 删除 $HOME → 回退 os.homedir()', () => {
  const prevHome = process.env.HOME
  try {
    delete process.env.HOME
    assert.strictEqual(homeDir(), os.homedir())
  } finally {
    if (prevHome === undefined) delete process.env.HOME
    else process.env.HOME = prevHome
  }
})

await runAll()
console.log(`test_home_dir: ${passed}/${tests.length} passed`)
if (passed !== tests.length) process.exit(1)
