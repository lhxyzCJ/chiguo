#!/usr/bin/env npx tsx
/**
 * Echo Bot — minimal example
 *
 * Demonstrates:
 *   - QR code login with callback
 *   - Middleware pipeline (logging + rate limiting)
 *   - Message handling with typing indicators
 *   - Graceful shutdown
 *
 * Usage:
 *   npx tsx examples/echo-bot.ts
 *   npx tsx examples/echo-bot.ts --force-login
 */

import {
  WeChatBot,
  loggingMiddleware,
  rateLimitMiddleware,
} from '../src/index.js'

const forceLogin = process.argv.includes('--force-login')

const bot = new WeChatBot({
  storage: 'file',       // Persist credentials & state across restarts
  logLevel: 'info',
})

// ── Middleware Pipeline ────────────────────────────────────────────────

// 1. Log every incoming message
bot.use(loggingMiddleware(bot.logger))

// 2. Rate limit: max 20 messages per user per minute
bot.use(rateLimitMiddleware({
  maxMessages: 20,
  windowMs: 60_000,
  logger: bot.logger,
}))

// ── Login ──────────────────────────────────────────────────────────────

const creds = await bot.login({
  force: forceLogin,
  callbacks: {
    onQrUrl: (url) => {
      console.log('\n╔══════════════════════════════════════════╗')
      console.log('║  Scan this QR code in WeChat to login:   ║')
      console.log('╚══════════════════════════════════════════╝')
      console.log(url)
      console.log()
    },
    onScanned: () => console.log('✓ QR scanned — confirm in WeChat'),
    onExpired: () => console.log('✗ QR expired — requesting new one...'),
  },
})

console.log(`\n✓ Logged in as ${creds.accountId}`)
console.log(`  User: ${creds.userId}`)
console.log(`  API:  ${creds.baseUrl}\n`)

// ── Message Handler ────────────────────────────────────────────────────

let messageCount = 0

bot.onMessage(async (msg) => {
  messageCount++

  try {
    // Show "typing..." while we process
    await bot.sendTyping(msg.userId)
  } catch {
    // typing failure shouldn't block the reply
  }

  // Simulate processing delay
  await new Promise((r) => setTimeout(r, 500))

  // Reply with echo
  await bot.reply(msg, `Echo: ${msg.text}`)
  console.log(`  → Replied to message #${messageCount}`)
})

// ── Lifecycle Events ───────────────────────────────────────────────────

bot.on('session:expired', () => {
  console.log('\n⚠ Session expired — re-login will happen automatically')
})

bot.on('session:restored', (newCreds) => {
  console.log(`✓ Session restored: ${newCreds.accountId}`)
})

bot.on('error', (err) => {
  console.error('⚠ Error:', err instanceof Error ? err.message : String(err))
})

// ── Graceful Shutdown ──────────────────────────────────────────────────

process.on('SIGINT', () => {
  console.log(`\nShutting down... (processed ${messageCount} messages)`)
  bot.stop()
})

// ── Start ──────────────────────────────────────────────────────────────

console.log('Listening for messages (Ctrl+C to stop)')
console.log('─'.repeat(50))
await bot.start()
console.log(`\nBot stopped. Processed ${messageCount} messages total.`)
