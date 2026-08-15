import { randomUUID } from 'node:crypto'
import {
  MessageItemType,
  MessageState,
  MessageType,
  type CDNMedia,
  type SendMessageRequest,
  type WireMessageItem,
} from '../protocol/types.js'

/**
 * Fluent builder for outgoing messages.
 * Supports composing text + media items before sending.
 *
 * Usage:
 *   const payload = MessageBuilder.to(userId, contextToken)
 *     .text("Here's your image:")
 *     .image({ media, midSize: 12345 })
 *     .build()
 */
export class MessageBuilder {
  private readonly items: WireMessageItem[] = []
  private clientId: string = randomUUID()

  private constructor(
    private readonly userId: string,
    private readonly contextToken: string,
  ) {}

  static to(userId: string, contextToken: string): MessageBuilder {
    return new MessageBuilder(userId, contextToken)
  }

  /** Set a custom client_id (default: random UUID). */
  withClientId(id: string): this {
    this.clientId = id
    return this
  }

  /** Add a text item. */
  text(content: string): this {
    this.items.push({
      type: MessageItemType.TEXT,
      text_item: { text: content },
    })
    return this
  }

  /** Add an image item with CDN media reference. */
  image(options: {
    media: CDNMedia
    midSize?: number
    thumbMedia?: CDNMedia
    thumbSize?: number
    thumbWidth?: number
    thumbHeight?: number
  }): this {
    this.items.push({
      type: MessageItemType.IMAGE,
      image_item: {
        media: options.media,
        mid_size: options.midSize,
        thumb_media: options.thumbMedia,
        thumb_size: options.thumbSize,
        thumb_width: options.thumbWidth,
        thumb_height: options.thumbHeight,
      },
    })
    return this
  }

  /** Add a file item with CDN media reference. */
  file(options: {
    media: CDNMedia
    fileName: string
    md5?: string
    size?: number
  }): this {
    this.items.push({
      type: MessageItemType.FILE,
      file_item: {
        media: options.media,
        file_name: options.fileName,
        md5: options.md5,
        len: options.size?.toString(),
      },
    })
    return this
  }

  /** Add a video item with CDN media reference. */
  video(options: {
    media: CDNMedia
    videoSize?: number
    playLength?: number
    thumbMedia?: CDNMedia
  }): this {
    this.items.push({
      type: MessageItemType.VIDEO,
      video_item: {
        media: options.media,
        video_size: options.videoSize,
        play_length: options.playLength,
        thumb_media: options.thumbMedia,
      },
    })
    return this
  }

  /** Build the wire-format message payload. */
  build(): SendMessageRequest['msg'] {
    if (this.items.length === 0) {
      throw new Error('Cannot build message with no items')
    }

    return {
      from_user_id: '',
      to_user_id: this.userId,
      client_id: this.clientId,
      message_type: MessageType.BOT,
      message_state: MessageState.FINISH,
      context_token: this.contextToken,
      item_list: this.items,
    }
  }
}
