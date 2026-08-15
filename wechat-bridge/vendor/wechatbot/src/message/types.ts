import type { WireMessage, CDNMedia } from '../protocol/types.js'

/**
 * Content type of an incoming message item.
 */
export type MessageContentType = 'text' | 'image' | 'voice' | 'file' | 'video'

/**
 * A parsed, user-friendly representation of an incoming message.
 * Hides protocol internals (context_token is managed by the SDK).
 */
export interface IncomingMessage {
  /** Sender's user ID (e.g. ...@im.wechat) */
  userId: string
  /** Extracted text content. For media, returns a description like [image]. */
  text: string
  /** Primary content type of the message. */
  type: MessageContentType
  /** Message creation timestamp. */
  timestamp: Date
  /** Image items in the message (if any). */
  images: ImageContent[]
  /** Voice items in the message (if any). */
  voices: VoiceContent[]
  /** File items in the message (if any). */
  files: FileContent[]
  /** Video items in the message (if any). */
  videos: VideoContent[]
  /** Referenced (quoted) message, if any. */
  quotedMessage?: QuotedMessage
  /** The raw wire message for advanced use. */
  raw: WireMessage

  // Internal — SDK-managed, not part of public contract
  /** @internal */ _contextToken: string
}

export interface ImageContent {
  media?: CDNMedia
  thumbMedia?: CDNMedia
  aeskey?: string
  url?: string
  width?: number
  height?: number
}

export interface VoiceContent {
  media?: CDNMedia
  text?: string
  durationMs?: number
  encodeType?: number
}

export interface FileContent {
  media?: CDNMedia
  fileName?: string
  md5?: string
  size?: number
}

export interface VideoContent {
  media?: CDNMedia
  thumbMedia?: CDNMedia
  durationMs?: number
  width?: number
  height?: number
}

export interface QuotedMessage {
  title?: string
  text?: string
  type?: MessageContentType
}
