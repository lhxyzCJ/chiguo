import type { LogEntry, Logger, LogLevel, LogTransport } from './types.js'

const LOG_LEVELS: Record<LogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
  silent: 4,
}

export class StderrTransport implements LogTransport {
  write(entry: LogEntry): void {
    const ts = entry.timestamp.toISOString()
    const ctx = entry.context ? ` [${entry.context}]` : ''
    const lvl = entry.level.toUpperCase().padEnd(5)
    const data = entry.data ? ` ${JSON.stringify(entry.data)}` : ''
    process.stderr.write(`${ts} ${lvl}${ctx} ${entry.message}${data}\n`)
  }
}

export class BotLogger implements Logger {
  private readonly levelThreshold: number

  constructor(
    private readonly transport: LogTransport,
    private readonly level: LogLevel = 'info',
    private readonly context?: string,
  ) {
    this.levelThreshold = LOG_LEVELS[level]
  }

  debug(message: string, data?: Record<string, unknown>): void {
    this.log('debug', message, data)
  }

  info(message: string, data?: Record<string, unknown>): void {
    this.log('info', message, data)
  }

  warn(message: string, data?: Record<string, unknown>): void {
    this.log('warn', message, data)
  }

  error(message: string, data?: Record<string, unknown>): void {
    this.log('error', message, data)
  }

  child(context: string): Logger {
    const prefix = this.context ? `${this.context}:${context}` : context
    return new BotLogger(this.transport, this.level, prefix)
  }

  private log(level: LogLevel, message: string, data?: Record<string, unknown>): void {
    if (LOG_LEVELS[level] < this.levelThreshold) return
    this.transport.write({
      level,
      message,
      timestamp: new Date(),
      context: this.context,
      data,
    })
  }
}

export function createLogger(options: { level?: LogLevel; transport?: LogTransport } = {}): Logger {
  const transport = options.transport ?? new StderrTransport()
  return new BotLogger(transport, options.level ?? 'info')
}
