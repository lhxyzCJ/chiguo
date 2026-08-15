/**
 * Abstract storage interface for SDK state persistence.
 * Implementations can be file-based, in-memory, Redis, SQLite, etc.
 */
export interface Storage {
  get<T>(key: string): Promise<T | undefined>
  set<T>(key: string, value: T): Promise<void>
  delete(key: string): Promise<void>
  has(key: string): Promise<boolean>
  clear(): Promise<void>
}

/** Well-known storage keys used by the SDK */
export const STORAGE_KEYS = {
  CREDENTIALS: 'credentials',
  CURSOR: 'cursor',
  CONTEXT_TOKENS: 'context_tokens',
  TYPING_TICKETS: 'typing_tickets',
} as const
