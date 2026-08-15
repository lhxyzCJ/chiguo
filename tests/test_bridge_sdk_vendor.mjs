// test_bridge_sdk_vendor.mjs
// v2.2.0（T23·Q27）— vendor 真实 SDK 契约测试：验证 CI 从 vendor 源码构建出的 @wechatbot/wechatbot
// 可被 import、实例化，并暴露 bridge.mjs 依赖的 API 契约。**不触网**：只测 import/实例化/导出形状，
// 登录与收发保持 mock（调用方在真实部署/运行期才连微信）。
// 前置：ci-test.sh 已构建 vendor 真实 SDK（wechat-bridge/node_modules/@wechatbot/wechatbot/dist/）。
import assert from 'node:assert'
import { readFileSync } from 'node:fs'

const mod = await import('../wechat-bridge/node_modules/@wechatbot/wechatbot/dist/index.js')

const { WeChatBot, MessageBuilder, TypedEmitter } = mod

// ── 1. import 真实 SDK 可解析出 WeChatBot ──
assert.ok(typeof WeChatBot === 'function', 'WeChatBot 应为构造器')

// ── 2. 实例化（memory 存储，不落盘、不触网）──
const bot = new WeChatBot({ storage: 'memory', logLevel: 'error' })
assert.ok(bot instanceof WeChatBot, 'new WeChatBot() 应为实例')
assert.ok(typeof bot === 'object', '实例应为对象')

// ── 3. bridge.mjs 依赖的 API 契约：构造器 + 实例方法 ──
// bridge.mjs 顶层 `import { WeChatBot }` + `new WeChatBot(...)`；协议上它后续会调用
// onMessage/login/start（真实收发）。这里只校验方法存在且为函数，不触发网络。
const instanceMethods = ['onMessage', 'login', 'start', 'reply', 'on', 'off', 'once', 'emit', 'send', 'stop']
for (const m of instanceMethods) {
  assert.strictEqual(typeof bot[m], 'function', `WeChatBot 实例缺少方法 ${m}()`)
}

// ── 4. 导出形状：bridge/测试可能用到的辅助导出 ──
assert.strictEqual(typeof MessageBuilder, 'function', '导出 MessageBuilder 应为构造器')
assert.strictEqual(typeof TypedEmitter, 'function', '导出 TypedEmitter 应为构造器')
for (const clsName of ['ApiError', 'AuthError', 'MediaError', 'NoContextError', 'TransportError', 'WeChatBotError']) {
  assert.strictEqual(typeof mod[clsName], 'function', `导出错误类 ${clsName} 应为构造器`)
}
// 子路径导出（media/middleware/storage）应可解析到已构建产物
const media = await import('../wechat-bridge/node_modules/@wechatbot/wechatbot/dist/media/index.js')
assert.ok(media && typeof media === 'object', 'media 子路径导出应可解析')
const middleware = await import('../wechat-bridge/node_modules/@wechatbot/wechatbot/dist/middleware/index.js')
assert.ok(middleware && typeof middleware === 'object', 'middleware 子路径导出应可解析')

// ── 5. LICENSE 合规随 vendor 入库（MIT 声明应存在）──
const license = readFileSync(new URL('../wechat-bridge/vendor/wechatbot/LICENSE', import.meta.url), 'utf8')
assert.ok(/MIT License/i.test(license), 'vendor 应随附 MIT LICENSE')

console.log('test_bridge_sdk_vendor: 通过')
