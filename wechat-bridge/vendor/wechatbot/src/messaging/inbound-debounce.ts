import type { IncomingMessage } from '../message/types.js'

export interface InboundDebounceOptions {
  /** Debounce window in milliseconds. 0 disables debouncing. */
  windowMs: number
  /** Separator used when joining buffered texts. Defaults to newline. */
  joinSeparator?: string
  /** Called with the merged message once the debounce window expires. */
  onFlush: (message: IncomingMessage) => void | Promise<void>
}

interface PendingBuffer {
  parts: string[]
  last: IncomingMessage
  timer: NodeJS.Timeout
}

/**
 * Trailing-window inbound debouncer.
 *
 * Batches rapid text messages from the same sender into a single message:
 * - Only plain text messages participate. Media, quoted and other content
 *   types flush the sender's buffer immediately and are never merged.
 * - The window resets on every buffered message (standard debounce).
 * - When the window expires, buffered texts are joined with `joinSeparator`
 *   and delivered via `onFlush` as one message (fields of the last message
 *   are kept, media lists are empty).
 */
export class InboundDebouncer {
  private readonly windowMs: number
  private readonly joinSeparator: string
  private readonly onFlush: (message: IncomingMessage) => void | Promise<void>
  private readonly buffers = new Map<string, PendingBuffer>()

  constructor(options: InboundDebounceOptions) {
    this.windowMs = Math.max(0, Math.trunc(options.windowMs))
    this.joinSeparator = options.joinSeparator ?? '\n'
    this.onFlush = options.onFlush
  }

  /**
   * Buffer a message if it is debounceable.
   * @returns true when the message was buffered (caller must NOT dispatch it),
   *          false when the message must be dispatched immediately (media /
   *          quoted / non-text content, or debounce disabled).
   */
  enqueue(message: IncomingMessage): boolean {
    if (this.windowMs === 0) return false
    if (!this.isDebounceable(message)) {
      // Media / quoted / non-text content: flush the sender's pending
      // texts immediately and let the caller dispatch this message as-is.
      this.flushUser(message.userId)
      return false
    }

    const userId = message.userId
    const existing = this.buffers.get(userId)

    if (existing) {
      existing.parts.push(message.text)
      existing.last = message
      clearTimeout(existing.timer)
      existing.timer = setTimeout(() => this.flushUser(userId), this.windowMs)
      existing.timer.unref?.()
    } else {
      const buffer: PendingBuffer = {
        parts: [message.text],
        last: message,
        timer: setTimeout(() => this.flushUser(userId), this.windowMs),
      }
      buffer.timer.unref?.()
      this.buffers.set(userId, buffer)
    }
    return true
  }

  /** Flush and dispatch the buffered messages of one sender (if any). */
  flushUser(userId: string): void {
    const buffer = this.buffers.get(userId)
    if (!buffer) return
    this.buffers.delete(userId)
    clearTimeout(buffer.timer)

    const merged: IncomingMessage = {
      ...buffer.last,
      text: buffer.parts.join(this.joinSeparator),
      images: [],
      voices: [],
      files: [],
      videos: [],
    }
    this.onFlush(merged)
  }

  /** Flush all pending buffers (e.g. on stop()). */
  flushAll(): void {
    for (const userId of [...this.buffers.keys()]) {
      this.flushUser(userId)
    }
  }

  /** Number of senders with pending buffered messages. */
  get pendingCount(): number {
    return this.buffers.size
  }

  private isDebounceable(message: IncomingMessage): boolean {
    if (message.type !== 'text') return false
    if (message.quotedMessage) return false
    return message.text.trim().length > 0
  }
}
