import { describe, it, expect } from 'vitest'
import { TypedEmitter } from '../src/core/events.js'

type TestEvents = {
  greet: [name: string]
  count: [n: number]
  empty: []
}

describe('TypedEmitter', () => {
  it('emits and receives events', () => {
    const emitter = new TypedEmitter<TestEvents>()
    const received: string[] = []

    emitter.on('greet', (name) => received.push(name))
    emitter.emit('greet', 'Alice')
    emitter.emit('greet', 'Bob')

    expect(received).toEqual(['Alice', 'Bob'])
  })

  it('supports multiple listeners', () => {
    const emitter = new TypedEmitter<TestEvents>()
    const a: string[] = []
    const b: string[] = []

    emitter.on('greet', (name) => a.push(name))
    emitter.on('greet', (name) => b.push(name))
    emitter.emit('greet', 'test')

    expect(a).toEqual(['test'])
    expect(b).toEqual(['test'])
  })

  it('removes listener with off', () => {
    const emitter = new TypedEmitter<TestEvents>()
    const received: string[] = []
    const listener = (name: string) => received.push(name)

    emitter.on('greet', listener)
    emitter.emit('greet', 'first')
    emitter.off('greet', listener)
    emitter.emit('greet', 'second')

    expect(received).toEqual(['first'])
  })

  it('once fires only once', () => {
    const emitter = new TypedEmitter<TestEvents>()
    let count = 0

    emitter.once('count', () => count++)
    emitter.emit('count', 1)
    emitter.emit('count', 2)

    expect(count).toBe(1)
  })

  it('handles zero-arg events', () => {
    const emitter = new TypedEmitter<TestEvents>()
    let fired = false

    emitter.on('empty', () => { fired = true })
    emitter.emit('empty')

    expect(fired).toBe(true)
  })

  it('removeAllListeners clears everything', () => {
    const emitter = new TypedEmitter<TestEvents>()
    let count = 0

    emitter.on('greet', () => count++)
    emitter.on('count', () => count++)
    emitter.removeAllListeners()
    emitter.emit('greet', 'test')
    emitter.emit('count', 1)

    expect(count).toBe(0)
  })

  it('removeAllListeners for specific event', () => {
    const emitter = new TypedEmitter<TestEvents>()
    let greetCount = 0
    let countCount = 0

    emitter.on('greet', () => greetCount++)
    emitter.on('count', () => countCount++)
    emitter.removeAllListeners('greet')
    emitter.emit('greet', 'test')
    emitter.emit('count', 1)

    expect(greetCount).toBe(0)
    expect(countCount).toBe(1)
  })

  it('listenerCount returns correct count', () => {
    const emitter = new TypedEmitter<TestEvents>()
    expect(emitter.listenerCount('greet')).toBe(0)

    emitter.on('greet', () => {})
    emitter.on('greet', () => {})
    expect(emitter.listenerCount('greet')).toBe(2)
  })

  it('swallows listener errors', () => {
    const emitter = new TypedEmitter<TestEvents>()
    const received: string[] = []

    emitter.on('greet', () => { throw new Error('boom') })
    emitter.on('greet', (name) => received.push(name))

    // Should not throw
    emitter.emit('greet', 'after-error')
    expect(received).toEqual(['after-error'])
  })
})
