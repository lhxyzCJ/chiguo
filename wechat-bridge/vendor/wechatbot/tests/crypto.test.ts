import { describe, it, expect } from 'vitest'
import {
  encryptAesEcb,
  decryptAesEcb,
  generateAesKey,
  encryptedSize,
  decodeAesKey,
  encodeAesKeyBase64,
  encodeAesKeyHex,
} from '../src/media/crypto.js'

describe('AES-128-ECB crypto', () => {
  it('encrypts and decrypts round-trip', () => {
    const key = generateAesKey()
    const plaintext = Buffer.from('Hello, WeChat!')
    const ciphertext = encryptAesEcb(plaintext, key)
    const decrypted = decryptAesEcb(ciphertext, key)
    expect(decrypted.toString()).toBe('Hello, WeChat!')
  })

  it('produces correct encrypted size', () => {
    const key = generateAesKey()

    // 14 bytes → ceil((14+1)/16)*16 = 16
    const data14 = Buffer.alloc(14)
    expect(encryptAesEcb(data14, key).length).toBe(16)
    expect(encryptedSize(14)).toBe(16)

    // 16 bytes → ceil((16+1)/16)*16 = 32
    const data16 = Buffer.alloc(16)
    expect(encryptAesEcb(data16, key).length).toBe(32)
    expect(encryptedSize(16)).toBe(32)

    // 100 bytes → ceil((100+1)/16)*16 = 112
    const data100 = Buffer.alloc(100)
    expect(encryptAesEcb(data100, key).length).toBe(112)
    expect(encryptedSize(100)).toBe(112)
  })

  it('rejects wrong key length', () => {
    const badKey = Buffer.alloc(8)
    expect(() => encryptAesEcb(Buffer.from('test'), badKey)).toThrow('16 bytes')
    expect(() => decryptAesEcb(Buffer.alloc(16), badKey)).toThrow('16 bytes')
  })
})

describe('AES key encoding', () => {
  it('decodes Format A: base64(raw 16 bytes)', () => {
    // Raw 16 bytes → base64
    const raw = Buffer.from('00112233445566778899aabbccddeeff', 'hex')
    const encoded = raw.toString('base64') // ABEiM0RVZneImaq7zN3u/w==
    const decoded = decodeAesKey(encoded)
    expect(decoded).toEqual(raw)
  })

  it('decodes Format B: base64(hex string)', () => {
    const hexStr = '00112233445566778899aabbccddeeff'
    const encoded = Buffer.from(hexStr, 'utf8').toString('base64')
    // MDAxMTIyMzM0NDU1NjY3Nzg4OTlhYWJiY2NkZGVlZmY=
    const decoded = decodeAesKey(encoded)
    expect(decoded).toEqual(Buffer.from(hexStr, 'hex'))
  })

  it('decodes direct hex string (image_item.aeskey)', () => {
    const hex = '00112233445566778899aabbccddeeff'
    const decoded = decodeAesKey(hex)
    expect(decoded).toEqual(Buffer.from(hex, 'hex'))
  })

  it('encodeAesKeyHex produces hex string', () => {
    const key = Buffer.from('00112233445566778899aabbccddeeff', 'hex')
    expect(encodeAesKeyHex(key)).toBe('00112233445566778899aabbccddeeff')
  })

  it('encodeAesKeyBase64 produces base64(hex) format', () => {
    const key = Buffer.from('00112233445566778899aabbccddeeff', 'hex')
    const result = encodeAesKeyBase64(key)
    // Should be base64 of the hex string
    expect(Buffer.from(result, 'base64').toString('ascii')).toBe(
      '00112233445566778899aabbccddeeff',
    )
  })
})
