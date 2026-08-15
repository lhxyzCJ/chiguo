import type { Logger } from '../logger/types.js'
import type { Middleware } from './types.js'

/**
 * Built-in middleware: log every incoming message.
 */
export function loggingMiddleware(logger: Logger): Middleware {
  const log = logger.child('msg')
  return async (ctx, next) => {
    const { message } = ctx
    log.info(`[${message.type}] ${message.userId}: ${message.text.slice(0, 100)}`)
    await next()
  }
}

/**
 * Built-in middleware: filter messages by text pattern.
 * If the pattern doesn't match, the message is skipped (next is not called).
 */
export function filterMiddleware(pattern: RegExp): Middleware {
  return async (ctx, next) => {
    if (pattern.test(ctx.message.text)) {
      await next()
    }
    // else: don't call next, message is filtered out
  }
}

/**
 * Built-in middleware: only process messages of certain types.
 */
export function typeFilterMiddleware(
  ...types: Array<'text' | 'image' | 'voice' | 'file' | 'video'>
): Middleware {
  const allowed = new Set(types)
  return async (ctx, next) => {
    if (allowed.has(ctx.message.type)) {
      await next()
    }
  }
}

/**
 * Built-in middleware: rate limit per user.
 * Drops messages if a user exceeds the limit within the window.
 */
export function rateLimitMiddleware(options: {
  maxMessages: number
  windowMs: number
  logger?: Logger
}): Middleware {
  const { maxMessages, windowMs, logger } = options
  const buckets = new Map<string, { count: number; resetAt: number }>()

  return async (ctx, next) => {
    const now = Date.now()
    const userId = ctx.message.userId
    let bucket = buckets.get(userId)

    if (!bucket || now >= bucket.resetAt) {
      bucket = { count: 0, resetAt: now + windowMs }
      buckets.set(userId, bucket)
    }

    bucket.count++

    if (bucket.count > maxMessages) {
      logger?.warn(`Rate limited user ${userId}`, {
        count: bucket.count,
        maxMessages,
      })
      ctx.handled = true
      return
    }

    await next()
  }
}
