export type LogLevel = 'debug' | 'info' | 'warn' | 'error' | 'silent'

export interface LogEntry {
  level: LogLevel
  message: string
  timestamp: Date
  context?: string
  data?: Record<string, unknown>
}

export interface LogTransport {
  write(entry: LogEntry): void
}

export interface Logger {
  debug(message: string, data?: Record<string, unknown>): void
  info(message: string, data?: Record<string, unknown>): void
  warn(message: string, data?: Record<string, unknown>): void
  error(message: string, data?: Record<string, unknown>): void
  child(context: string): Logger
}
