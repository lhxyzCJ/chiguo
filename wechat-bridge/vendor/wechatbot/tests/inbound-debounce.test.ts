import { describe, it, expect, vi } from 'vitest'
import { InboundDebouncer } from '../src/messaging/inbound-debounce.js'
import type { IncomingMessage } from '../src/message/types.js'

function makeMessage(text: string, opts: Partial<IncomingMessage> = {}): IncomingMessage {
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
    ...opts,
  }
}

async function tick(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms))
}

describe('InboundDebouncer', () => {
  it('merges rapid same-sender texts into a single flush', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 50,
      onFlush: (m) => flushed.push(m),
    })

    expect(d.enqueue(makeMessage('msg1'))).toBe(true)
    await tick(10)
    expect(d.enqueue(makeMessage('msg2'))).toBe(true)
    await tick(10)
    expect(d.enqueue(makeMessage('msg3'))).toBe(true)

    expect(flushed).toHaveLength(0)
    await tick(80)
    expect(flushed).toHaveLength(1)
    expect(flushed[0]!.text).toBe('msg1\nmsg2\nmsg3')
    expect(d.pendingCount).toBe(0)
  })

  it('does not merge different senders', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 30,
      onFlush: (m) => flushed.push(m),
    })

    d.enqueue(makeMessage('a1', { userId: 'user-a' }))
    d.enqueue(makeMessage('b1', { userId: 'user-b' }))
    await tick(60)
    expect(flushed).toHaveLength(2)
    expect(flushed.map((m) => m.text).sort()).toEqual(['a1', 'b1'])
  })

  it('never merges media messages and flushes the sender buffer', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 1000,
      onFlush: (m) => flushed.push(m),
    })

    expect(d.enqueue(makeMessage('text first'))).toBe(true)
    expect(d.enqueue(makeMessage('[image]', { type: 'image' as const, images: [{}] as any }))).toBe(false)
    expect(flushed).toHaveLength(1)
    expect(flushed[0]!.text).toBe('text first')
    expect(d.pendingCount).toBe(0)
  })

  it('never merges quoted messages', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 1000,
      onFlush: (m) => flushed.push(m),
    })

    expect(d.enqueue(makeMessage('reply', { quotedMessage: { refMessageId: 'x' } as any }))).toBe(false)
    expect(flushed).toHaveLength(0)
  })

  it('keeps fields of the last message and clears media lists on merge', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 30,
      onFlush: (m) => flushed.push(m),
    })

    const last = makeMessage('second', { userId: 'u', timestamp: new Date(2000), _contextToken: 'latest-token' })
    d.enqueue(makeMessage('first', { userId: 'u', timestamp: new Date(1000), _contextToken: 'old-token' }))
    d.enqueue(last)
    await tick(60)

    expect(flushed).toHaveLength(1)
    const merged = flushed[0]!
    expect(merged.text).toBe('first\nsecond')
    expect(merged._contextToken).toBe('latest-token')
    expect(merged.timestamp.getTime()).toBe(2000)
    expect(merged.images).toEqual([])
    expect(merged.voices).toEqual([])
    expect(merged.files).toEqual([])
    expect(merged.videos).toEqual([])
  })

  it('supports a custom join separator', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 30,
      joinSeparator: ' / ',
      onFlush: (m) => flushed.push(m),
    })

    d.enqueue(makeMessage('a'))
    d.enqueue(makeMessage('b'))
    await tick(60)
    expect(flushed[0]!.text).toBe('a / b')
  })

  it('returns false (no buffering) when windowMs is 0', () => {
    const d = new InboundDebouncer({
      windowMs: 0,
      onFlush: () => {},
    })
    expect(d.enqueue(makeMessage('x'))).toBe(false)
    expect(d.pendingCount).toBe(0)
  })

  it('flushAll delivers all pending buffers immediately', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 10000,
      onFlush: (m) => flushed.push(m),
    })

    d.enqueue(makeMessage('a'))
    d.enqueue(makeMessage('b'))
    expect(d.pendingCount).toBe(1)

    d.flushAll()
    expect(flushed).toHaveLength(1)
    expect(flushed[0]!.text).toBe('a\nb')
    expect(d.pendingCount).toBe(0)
  })

  it('flushUser only flushes that sender', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 10000,
      onFlush: (m) => flushed.push(m),
    })

    d.enqueue(makeMessage('a', { userId: 'u1' }))
    d.enqueue(makeMessage('b', { userId: 'u2' }))
    d.flushUser('u1')

    expect(flushed.map((m) => m.userId)).toEqual(['u1'])
    expect(d.pendingCount).toBe(1)
    d.flushAll()
    expect(flushed).toHaveLength(2)
  })

  it('window resets on each buffered message', async () => {
    const flushed: IncomingMessage[] = []
    const d = new InboundDebouncer({
      windowMs: 40,
      onFlush: (m) => flushed.push(m),
    })

    d.enqueue(makeMessage('a'))
    await tick(25)
    d.enqueue(makeMessage('b'))
    await tick(25)
    d.enqueue(makeMessage('c'))
    await tick(25)

    expect(flushed).toHaveLength(0) // c 重置了窗口
    await tick(60)
    expect(flushed).toHaveLength(1)
    expect(flushed[0]!.text).toBe('a\nb\nc')
  })

  it('delivers each buffered flush to the onFlush callback', () => {
    // Regression guard: onFlush is async-safe, called exactly once per flush
    const flush = vi.fn()
    const d = new InboundDebouncer({ windowMs: 30, onFlush: flush })
    d.enqueue(makeMessage('x'))
    d.flushUser('test-user')
    expect(flush).toHaveBeenCalledTimes(1)
  })
})
