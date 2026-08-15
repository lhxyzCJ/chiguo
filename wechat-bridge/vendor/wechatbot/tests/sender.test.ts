import { describe, it, expect } from 'vitest'
import { chunkText } from '../src/messaging/sender.js'

describe('chunkText', () => {
  it('returns single chunk for short text', () => {
    expect(chunkText('hello', 2000)).toEqual(['hello'])
  })

  it('returns empty string for empty text', () => {
    expect(chunkText('', 2000)).toEqual([''])
  })

  it('splits at paragraph breaks', () => {
    const text = 'A'.repeat(1500) + '\n\n' + 'B'.repeat(1000)
    const chunks = chunkText(text, 2000)
    expect(chunks).toHaveLength(2)
    expect(chunks[0]).toBe('A'.repeat(1500) + '\n\n')
    expect(chunks[1]).toBe('B'.repeat(1000))
  })

  it('splits at line breaks when no paragraph break', () => {
    const text = 'A'.repeat(1500) + '\n' + 'B'.repeat(1000)
    const chunks = chunkText(text, 2000)
    expect(chunks).toHaveLength(2)
    expect(chunks[0]).toBe('A'.repeat(1500) + '\n')
  })

  it('hard cuts when no natural break', () => {
    const text = 'A'.repeat(3000)
    const chunks = chunkText(text, 2000)
    expect(chunks).toHaveLength(2)
    expect(chunks[0]).toBe('A'.repeat(2000))
    expect(chunks[1]).toBe('A'.repeat(1000))
  })

  it('handles multiple chunks', () => {
    const text = 'A'.repeat(5000)
    const chunks = chunkText(text, 2000)
    expect(chunks).toHaveLength(3)
    expect(chunks.join('')).toBe(text)
  })
})
