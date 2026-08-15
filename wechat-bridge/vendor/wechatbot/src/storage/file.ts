import { mkdir, readFile, writeFile, rm, readdir } from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import type { Storage } from './interface.js'

const DEFAULT_DIR = path.join(os.homedir(), '.wechatbot')

/**
 * File-based storage — each key is a JSON file.
 * Credentials and state survive restarts.
 */
export class FileStorage implements Storage {
  private readonly dir: string

  constructor(dir?: string) {
    this.dir = dir ?? DEFAULT_DIR
  }

  async get<T>(key: string): Promise<T | undefined> {
    try {
      const raw = await readFile(this.filePath(key), 'utf8')
      return JSON.parse(raw) as T
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === 'ENOENT') return undefined
      throw err
    }
  }

  async set<T>(key: string, value: T): Promise<void> {
    await this.ensureDir()
    await writeFile(this.filePath(key), JSON.stringify(value, null, 2) + '\n', {
      mode: 0o600,
    })
  }

  async delete(key: string): Promise<void> {
    await rm(this.filePath(key), { force: true })
  }

  async has(key: string): Promise<boolean> {
    try {
      await readFile(this.filePath(key))
      return true
    } catch {
      return false
    }
  }

  async clear(): Promise<void> {
    try {
      const files = await readdir(this.dir)
      await Promise.all(
        files
          .filter((f) => f.endsWith('.json'))
          .map((f) => rm(path.join(this.dir, f), { force: true })),
      )
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code !== 'ENOENT') throw err
    }
  }

  private filePath(key: string): string {
    // Sanitize key for filesystem safety
    const safe = key.replace(/[^a-zA-Z0-9_-]/g, '_')
    return path.join(this.dir, `${safe}.json`)
  }

  private async ensureDir(): Promise<void> {
    await mkdir(this.dir, { recursive: true, mode: 0o700 })
  }
}
