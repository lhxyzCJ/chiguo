import { describe, it, expect } from 'vitest'
import { MemoryStorage } from '../src/storage/memory.js'

describe('MemoryStorage', () => {
  it('stores and retrieves values', async () => {
    const store = new MemoryStorage()
    await store.set('key', { hello: 'world' })
    expect(await store.get('key')).toEqual({ hello: 'world' })
  })

  it('returns undefined for missing keys', async () => {
    const store = new MemoryStorage()
    expect(await store.get('missing')).toBeUndefined()
  })

  it('deletes values', async () => {
    const store = new MemoryStorage()
    await store.set('key', 'value')
    await store.delete('key')
    expect(await store.get('key')).toBeUndefined()
  })

  it('checks existence with has', async () => {
    const store = new MemoryStorage()
    expect(await store.has('key')).toBe(false)
    await store.set('key', 'value')
    expect(await store.has('key')).toBe(true)
  })

  it('clears all values', async () => {
    const store = new MemoryStorage()
    await store.set('a', 1)
    await store.set('b', 2)
    await store.clear()
    expect(await store.has('a')).toBe(false)
    expect(await store.has('b')).toBe(false)
  })
})
