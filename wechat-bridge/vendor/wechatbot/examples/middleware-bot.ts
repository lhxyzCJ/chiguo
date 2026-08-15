#!/usr/bin/env npx tsx
/**
 * Middleware Bot — demonstrates the full middleware pipeline
 *
 * Shows how to:
 *   - Write custom middleware
 *   - Chain middleware (filter → transform → handle)
 *   - Use the state bag to pass data between middleware
 *   - Type-filter messages
 *   - Handle different message types
 */

import {
  WeChatBot,
  typeFilterMiddleware,
  type Middleware,
  type IncomingMessage,
} from '../src/index.js'

const bot = new WeChatBot({ storage: 'file', logLevel: 'debug' })

// ── Custom Middleware: Command Parser ──────────────────────────────────

const commandParser: Middleware = async (ctx, next) => {
  const text = ctx.message.text.trim()

  if (text.startsWith('/')) {
    const [command, ...args] = text.slice(1).split(/\s+/)
    ctx.state.set('command', command?.toLowerCase())
    ctx.state.set('args', args)
  }

  await next()
}

// ── Custom Middleware: Ignore List ─────────────────────────────────────

const ignoreList = new Set<string>()

const ignoreMiddleware: Middleware = async (ctx, next) => {
  if (ignoreList.has(ctx.message.userId)) {
    ctx.handled = true // Stop processing
    return
  }
  await next()
}

// ── Custom Middleware: Response Timer ──────────────────────────────────

const timerMiddleware: Middleware = async (ctx, next) => {
  const start = Date.now()
  await next()
  const elapsed = Date.now() - start
  bot.logger.info(`Response time: ${elapsed}ms`, { userId: ctx.message.userId })
}

// ── Register Middleware Chain ──────────────────────────────────────────

bot
  .use(timerMiddleware)               // 1. Time the full pipeline
  .use(ignoreMiddleware)              // 2. Check ignore list
  .use(typeFilterMiddleware('text'))   // 3. Only process text messages
  .use(commandParser)                 // 4. Parse commands

// ── Message Handler ────────────────────────────────────────────────────

bot.onMessage(async (msg: IncomingMessage) => {
  const command = bot as any // We need to access middleware state differently

  // Check if this was a command (set by commandParser middleware)
  // Since middleware state isn't directly accessible in handlers,
  // we'll parse commands here as well
  const text = msg.text.trim()

  if (text.startsWith('/')) {
    const [cmd, ...args] = text.slice(1).split(/\s+/)

    switch (cmd?.toLowerCase()) {
      case 'help':
        await bot.reply(msg, [
          '📋 Available commands:',
          '/help — Show this message',
          '/ping — Check if bot is alive',
          '/echo <text> — Echo back text',
          '/time — Current server time',
          '/stats — Bot statistics',
        ].join('\n'))
        return

      case 'ping':
        await bot.reply(msg, '🏓 Pong!')
        return

      case 'echo':
        await bot.reply(msg, args.join(' ') || '(empty)')
        return

      case 'time':
        await bot.reply(msg, `🕐 ${new Date().toISOString()}`)
        return

      case 'stats':
        await bot.reply(msg, [
          '📊 Bot Stats:',
          `  Uptime: ${Math.floor(process.uptime())}s`,
          `  Memory: ${Math.round(process.memoryUsage().heapUsed / 1024 / 1024)}MB`,
        ].join('\n'))
        return

      default:
        await bot.reply(msg, `Unknown command: /${cmd}\nType /help for available commands.`)
        return
    }
  }

  // Non-command messages: simple echo
  await bot.reply(msg, `You said: ${text}`)
})

// ── Start ──────────────────────────────────────────────────────────────

await bot.run({
  callbacks: {
    onQrUrl: (url) => console.log(`\nScan to login: ${url}\n`),
  },
})
