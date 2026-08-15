import { MessageItemType, MessageType, type WireMessage, type WireMessageItem } from '../protocol/types.js'
import type {
  FileContent,
  ImageContent,
  IncomingMessage,
  MessageContentType,
  QuotedMessage,
  VideoContent,
  VoiceContent,
} from './types.js'

/**
 * Parses raw wire messages into user-friendly IncomingMessage objects.
 * Extracts structured content from the item_list.
 */
export class MessageParser {
  /**
   * Parse a wire message into an IncomingMessage.
   * Returns null if the message should be filtered (e.g. bot's own messages).
   */
  parse(wire: WireMessage): IncomingMessage | null {
    // Only process messages from users, not bot echo
    if (wire.message_type !== MessageType.USER) {
      return null
    }

    const images: ImageContent[] = []
    const voices: VoiceContent[] = []
    const files: FileContent[] = []
    const videos: VideoContent[] = []
    let quotedMessage: QuotedMessage | undefined

    for (const item of wire.item_list) {
      if (item.image_item) {
        images.push({
          media: item.image_item.media,
          thumbMedia: item.image_item.thumb_media,
          aeskey: item.image_item.aeskey,
          url: item.image_item.url,
          width: item.image_item.thumb_width,
          height: item.image_item.thumb_height,
        })
      }

      if (item.voice_item) {
        voices.push({
          media: item.voice_item.media,
          text: item.voice_item.text,
          durationMs: item.voice_item.playtime,
          encodeType: item.voice_item.encode_type,
        })
      }

      if (item.file_item) {
        files.push({
          media: item.file_item.media,
          fileName: item.file_item.file_name,
          md5: item.file_item.md5,
          size: item.file_item.len ? parseInt(item.file_item.len, 10) : undefined,
        })
      }

      if (item.video_item) {
        videos.push({
          media: item.video_item.media,
          thumbMedia: item.video_item.thumb_media,
          durationMs: item.video_item.play_length,
          width: item.video_item.thumb_width,
          height: item.video_item.thumb_height,
        })
      }

      if (item.ref_msg) {
        quotedMessage = parseRefMessage(item.ref_msg)
      }
    }

    return {
      userId: wire.from_user_id,
      text: extractText(wire.item_list),
      type: detectPrimaryType(wire.item_list),
      timestamp: new Date(wire.create_time_ms),
      images,
      voices,
      files,
      videos,
      quotedMessage,
      raw: wire,
      _contextToken: wire.context_token,
    }
  }
}

function detectPrimaryType(items: WireMessageItem[]): MessageContentType {
  const first = items[0]
  if (!first) return 'text'

  switch (first.type) {
    case MessageItemType.IMAGE:
      return 'image'
    case MessageItemType.VOICE:
      return 'voice'
    case MessageItemType.FILE:
      return 'file'
    case MessageItemType.VIDEO:
      return 'video'
    default:
      return 'text'
  }
}

function extractText(items: WireMessageItem[]): string {
  const parts: string[] = []

  for (const item of items) {
    switch (item.type) {
      case MessageItemType.TEXT:
        if (item.text_item?.text) parts.push(item.text_item.text)
        break
      case MessageItemType.IMAGE:
        parts.push(item.image_item?.url ?? '[image]')
        break
      case MessageItemType.VOICE:
        parts.push(item.voice_item?.text ?? '[voice]')
        break
      case MessageItemType.FILE:
        parts.push(item.file_item?.file_name ?? '[file]')
        break
      case MessageItemType.VIDEO:
        parts.push('[video]')
        break
    }
  }

  return parts.join('\n')
}

function parseRefMessage(ref: { title?: string; message_item?: WireMessageItem }): QuotedMessage {
  const result: QuotedMessage = { title: ref.title }

  if (ref.message_item) {
    result.type = detectPrimaryType([ref.message_item])
    if (ref.message_item.text_item?.text) {
      result.text = ref.message_item.text_item.text
    }
  }

  return result
}
