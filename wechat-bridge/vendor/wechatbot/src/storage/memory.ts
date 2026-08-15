import type { Storage } from './interface.js'

/**
 * In-memory storage. Fast, but state is lost on restart.
 * Good for testing and ephemeral bots.
 */
export class MemoryStorage implements Storage {
  private readonly store = new Map<string, unknown>()

  async get<T>(key: string): Promise<T | undefined> {
    return this.store.get(key) as T | undefined
  }

  async set<T>(key: string, value: T): Promise<void> {
    this.store.set(key, value)
  }

  async delete(key: string): Promise<void> {
    this.store.delete(key)
  }

  async has(key: string): Promise<boolean> {
    return this.store.has(key)
  }

  async clear(): Promise<void> {
    this.store.clear()
  }
}
