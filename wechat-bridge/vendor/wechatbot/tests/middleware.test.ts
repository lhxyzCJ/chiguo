import { describe, it, expect } from 'vitest'
import { MiddlewareEngine } from '../src/middleware/engine.js'
import type { IncomingMessage } from '../src/message/types.js'
import type { MessageContext } from '../src/middleware/types.js'

function makeMessage(text: string): IncomingMessage {
  return {
    userId: 'test-user',
    text,
    type: 'text',
    timestamp: new Date(),
    images: [],
    voices: [],
    files: [],
    videos: [],
    raw: {} as any,
    _contextToken: 'test-token',
  }
}

describe('MiddlewareEngine', () => {
  it('executes middleware in order', async () => {
    const engine = new MiddlewareEngine()
    const order: number[] = []

    engine.use(async (_ctx, next) => { order.push(1); await next() })
    engine.use(async (_ctx, next) => { order.push(2); await next() })
    engine.use(async (_ctx, next) => { order.push(3); await next() })

    await engine.run(makeMessage('hello'))
    expect(order).toEqual([1, 2, 3])
  })

  it('stops chain when middleware does not call next', async () => {
    const engine = new MiddlewareEngine()
    const order: number[] = []

    engine.use(async (_ctx, _next) => { order.push(1) }) // No next()!
    engine.use(async (_ctx, next) => { order.push(2); await next() })

    await engine.run(makeMessage('hello'))
    expect(order).toEqual([1]) // Only first ran
  })

  it('stops chain when handled is set', async () => {
    const engine = new MiddlewareEngine()
    const order: number[] = []

    engine.use(async (ctx, next) => {
      ctx.handled = true
      order.push(1)
      await next()
    })
    engine.use(async (_ctx, next) => { order.push(2); await next() })

    const ctx = await engine.run(makeMessage('hello'))
    expect(order).toEqual([1])
    expect(ctx.handled).toBe(true)
  })

  it('allows state sharing between middleware', async () => {
    const engine = new MiddlewareEngine()

    engine.use(async (ctx, next) => {
      ctx.state.set('key', 42)
      await next()
    })

    engine.use(async (ctx, next) => {
      const val = ctx.state.get('key')
      ctx.state.set('result', (val as number) * 2)
      await next()
    })

    const ctx = await engine.run(makeMessage('hello'))
    expect(ctx.state.get('result')).toBe(84)
  })

  it('handles empty middleware stack', async () => {
    const engine = new MiddlewareEngine()
    const ctx = await engine.run(makeMessage('hello'))
    expect(ctx.handled).toBe(false)
  })

  it('prevents double-calling next', async () => {
    const engine = new MiddlewareEngine()
    let count = 0

    engine.use(async (_ctx, next) => {
      await next()
      await next() // second call should be a no-op
    })
    engine.use(async (_ctx, next) => {
      count++
      await next()
    })

    await engine.run(makeMessage('hello'))
    expect(count).toBe(1)
  })
})
