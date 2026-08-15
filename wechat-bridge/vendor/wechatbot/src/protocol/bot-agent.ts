import { CHANNEL_VERSION } from './types.js'

/** Default `bot_agent` when none is configured or the configured value is invalid. */
export const DEFAULT_BOT_AGENT = `WeChatBot/${CHANNEL_VERSION}`

/** Maximum length (bytes) of the sanitized `bot_agent` string. */
const BOT_AGENT_MAX_LEN = 256

/**
 * UA-style grammar (matches openclaw-weixin):
 *   bot_agent = product *( SP product )
 *   product   = name "/" version [ SP "(" comment ")" ]
 *   name      = 1*32( ALPHA / DIGIT / "_" / "." / "-" )
 *   version   = 1*32( ALPHA / DIGIT / "_" / "." / "+" / "-" )
 *   comment   = 1*64( printable ASCII minus "(" ")" )
 */
const PRODUCT = String.raw`[A-Za-z0-9_.\-]{1,32}\/[A-Za-z0-9_.+\-]{1,32}(?: \([\x20-\x27\x2A-\x7E]{1,64}\))?`
const BOT_AGENT_RE = new RegExp(`^${PRODUCT}(?: ${PRODUCT})*$`)

/**
 * Validate a user-supplied `botAgent` config value into a wire-safe string.
 *
 * Unlike upstream openclaw-weixin (which salvages the valid tokens out of a
 * partially invalid string), any invalid input falls back to
 * `DEFAULT_BOT_AGENT` wholesale — simpler and just as safe on the wire.
 */
export function sanitizeBotAgent(raw?: string): string {
  const trimmed = raw?.trim()
  if (!trimmed) return DEFAULT_BOT_AGENT
  const normalized = trimmed.replace(/\s+/g, ' ')
  if (Buffer.byteLength(normalized, 'utf-8') > BOT_AGENT_MAX_LEN) return DEFAULT_BOT_AGENT
  return BOT_AGENT_RE.test(normalized) ? normalized : DEFAULT_BOT_AGENT
}
