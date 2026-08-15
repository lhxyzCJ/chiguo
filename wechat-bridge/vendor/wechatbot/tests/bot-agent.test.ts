import { describe, expect, it } from 'vitest'
import { DEFAULT_BOT_AGENT, sanitizeBotAgent } from '../src/protocol/bot-agent.js'

describe('sanitizeBotAgent', () => {
  it('falls back to default for empty input', () => {
    expect(sanitizeBotAgent(undefined)).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent('')).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent('   ')).toBe(DEFAULT_BOT_AGENT)
  })

  it('accepts a single product', () => {
    expect(sanitizeBotAgent('MyApp/1.2')).toBe('MyApp/1.2')
  })

  it('accepts a product with a comment', () => {
    expect(sanitizeBotAgent('MyApp/1.2 (prod build)')).toBe('MyApp/1.2 (prod build)')
  })

  it('accepts multiple products', () => {
    expect(sanitizeBotAgent('MyApp/1.2 (prod) Lib/0.3')).toBe('MyApp/1.2 (prod) Lib/0.3')
  })

  it('normalizes extra whitespace', () => {
    expect(sanitizeBotAgent('  MyApp/1.2   Lib/0.3 ')).toBe('MyApp/1.2 Lib/0.3')
  })

  it('rejects invalid input wholesale', () => {
    expect(sanitizeBotAgent('no-slash')).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent('bad name/1.0 !!!')).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent('(orphan comment)')).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent('App/1.0 (unclosed')).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent('App/1.0 (nested (comment))')).toBe(DEFAULT_BOT_AGENT)
  })

  it('rejects names or versions that are too long', () => {
    expect(sanitizeBotAgent(`${'a'.repeat(33)}/1.0`)).toBe(DEFAULT_BOT_AGENT)
    expect(sanitizeBotAgent(`App/${'1'.repeat(33)}`)).toBe(DEFAULT_BOT_AGENT)
  })

  it('rejects strings over the byte cap', () => {
    const product = 'App/1.0 '
    expect(sanitizeBotAgent(product.repeat(40).trim())).toBe(DEFAULT_BOT_AGENT)
  })
})
