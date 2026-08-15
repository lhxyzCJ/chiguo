import { describe, it, expect } from 'vitest'
import { MessageParser } from '../src/message/parser.js'
import { MessageType, MessageState, MessageItemType, type WireMessage } from '../src/protocol/types.js'

const parser = new MessageParser()

function makeWireMessage(overrides: Partial<WireMessage> = {}): WireMessage {
  return {
    from_user_id: 'user@im.wechat',
    to_user_id: 'bot@im.bot',
    client_id: 'test-client-id',
    create_time_ms: 1700000000000,
    message_type: MessageType.USER,
    message_state: MessageState.FINISH,
    context_token: 'test-ctx-token',
    item_list: [
      { type: MessageItemType.TEXT, text_item: { text: 'Hello' } },
    ],
    ...overrides,
  }
}

describe('MessageParser', () => {
  it('parses a text message', () => {
    const msg = parser.parse(makeWireMessage())
    expect(msg).not.toBeNull()
    expect(msg!.userId).toBe('user@im.wechat')
    expect(msg!.text).toBe('Hello')
    expect(msg!.type).toBe('text')
    expect(msg!._contextToken).toBe('test-ctx-token')
  })

  it('returns null for BOT messages', () => {
    const msg = parser.parse(makeWireMessage({ message_type: MessageType.BOT }))
    expect(msg).toBeNull()
  })

  it('parses image messages', () => {
    const msg = parser.parse(makeWireMessage({
      item_list: [{
        type: MessageItemType.IMAGE,
        image_item: {
          media: { encrypt_query_param: 'test', aes_key: 'key' },
          aeskey: '00112233445566778899aabbccddeeff',
          url: 'https://example.com/img.jpg',
          thumb_width: 320,
          thumb_height: 240,
        },
      }],
    }))

    expect(msg!.type).toBe('image')
    expect(msg!.images).toHaveLength(1)
    expect(msg!.images[0]!.aeskey).toBe('00112233445566778899aabbccddeeff')
    expect(msg!.images[0]!.width).toBe(320)
  })

  it('parses voice messages with transcription', () => {
    const msg = parser.parse(makeWireMessage({
      item_list: [{
        type: MessageItemType.VOICE,
        voice_item: {
          media: { encrypt_query_param: 'test', aes_key: 'key' },
          text: 'Hello there',
          playtime: 3000,
          encode_type: 6,
        },
      }],
    }))

    expect(msg!.type).toBe('voice')
    expect(msg!.voices).toHaveLength(1)
    expect(msg!.voices[0]!.text).toBe('Hello there')
    expect(msg!.voices[0]!.durationMs).toBe(3000)
  })

  it('parses file messages', () => {
    const msg = parser.parse(makeWireMessage({
      item_list: [{
        type: MessageItemType.FILE,
        file_item: {
          media: { encrypt_query_param: 'test', aes_key: 'key' },
          file_name: 'report.pdf',
          md5: 'abc123',
          len: '542188',
        },
      }],
    }))

    expect(msg!.type).toBe('file')
    expect(msg!.files).toHaveLength(1)
    expect(msg!.files[0]!.fileName).toBe('report.pdf')
    expect(msg!.files[0]!.size).toBe(542188)
  })

  it('parses quoted messages', () => {
    const msg = parser.parse(makeWireMessage({
      item_list: [{
        type: MessageItemType.TEXT,
        text_item: { text: 'My reply' },
        ref_msg: {
          title: 'Quoted message',
          message_item: {
            type: MessageItemType.TEXT,
            text_item: { text: 'Original text' },
          },
        },
      }],
    }))

    expect(msg!.quotedMessage).toBeDefined()
    expect(msg!.quotedMessage!.title).toBe('Quoted message')
    expect(msg!.quotedMessage!.text).toBe('Original text')
  })

  it('extracts text from mixed content', () => {
    const msg = parser.parse(makeWireMessage({
      item_list: [
        { type: MessageItemType.TEXT, text_item: { text: 'Look at this:' } },
        { type: MessageItemType.IMAGE, image_item: { url: 'https://img.com/1.jpg' } },
      ],
    }))

    expect(msg!.text).toBe('Look at this:\nhttps://img.com/1.jpg')
  })
})
