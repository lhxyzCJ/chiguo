import { createCipheriv, createDecipheriv, randomBytes } from 'node:crypto'
import { MediaError } from '../core/errors.js'

/**
 * AES-128-ECB encryption used by WeChat CDN.
 * All media files are encrypted before upload and decrypted after download.
 */
export function encryptAesEcb(plaintext: Buffer, key: Buffer): Buffer {
  if (key.length !== 16) {
    throw new MediaError(`AES key must be 16 bytes, got ${key.length}`)
  }
  const cipher = createCipheriv('aes-128-ecb', key, null)
  return Buffer.concat([cipher.update(plaintext), cipher.final()])
}

export function decryptAesEcb(ciphertext: Buffer, key: Buffer): Buffer {
  if (key.length !== 16) {
    throw new MediaError(`AES key must be 16 bytes, got ${key.length}`)
  }
  const decipher = createDecipheriv('aes-128-ecb', key, null)
  return Buffer.concat([decipher.update(ciphertext), decipher.final()])
}

/**
 * Generate a random 16-byte AES key.
 */
export function generateAesKey(): Buffer {
  return randomBytes(16)
}

/**
 * Calculate the encrypted file size (AES-128-ECB with PKCS7 padding).
 * Formula: ceil((rawsize + 1) / 16) * 16
 */
export function encryptedSize(rawSize: number): number {
  return Math.ceil((rawSize + 1) / 16) * 16
}

/**
 * Decode an aes_key from the protocol.
 * Handles both formats:
 *   - Format A: base64(raw 16 bytes) → 16-byte key
 *   - Format B: base64(hex string 32 chars) → hex decode → 16-byte key
 *   - Direct hex string (32 chars) from image_item.aeskey
 */
export function decodeAesKey(encoded: string): Buffer {
  // Try direct hex first (image_item.aeskey)
  if (/^[0-9a-fA-F]{32}$/.test(encoded)) {
    return Buffer.from(encoded, 'hex')
  }

  // Try base64 decode
  const decoded = Buffer.from(encoded, 'base64')

  if (decoded.length === 16) {
    // Format A: raw 16 bytes
    return decoded
  }

  if (decoded.length === 32) {
    // Format B: 32 hex ASCII chars
    const hex = decoded.toString('ascii')
    if (/^[0-9a-fA-F]{32}$/.test(hex)) {
      return Buffer.from(hex, 'hex')
    }
  }

  throw new MediaError(
    `Cannot decode AES key: base64-decoded length is ${decoded.length} (expected 16 or 32)`,
  )
}

/**
 * Encode an AES key for the getuploadurl request (hex string format).
 */
export function encodeAesKeyHex(key: Buffer): string {
  return key.toString('hex')
}

/**
 * Encode an AES key for the sendmessage CDNMedia.aes_key field.
 * Uses base64(hex string) format for compatibility with official implementation.
 */
export function encodeAesKeyBase64(key: Buffer): string {
  return Buffer.from(key.toString('hex'), 'utf8').toString('base64')
}
