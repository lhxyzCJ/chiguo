import { NoContextError } from '../core/errors.js'
import type { Logger } from '../logger/types.js'
import type { ILinkApi } from '../protocol/api.js'
import type { ContextStore } from './context.js'

/**
 * Typing indicator service.
 * Manages the getconfig → sendtyping flow with ticket caching.
 */
export class TypingService {
  private readonly logger: Logger
  /** Cache typing tickets to avoid repeated getconfig calls */
  private readonly ticketCache = new Map<string, { ticket: string; expiresAt: number }>()
  private static readonly TICKET_TTL_MS = 24 * 60 * 60 * 1_000 // 24 hours

  constructor(
    private readonly api: ILinkApi,
    private readonly contextStore: ContextStore,
    logger: Logger,
  ) {
    this.logger = logger.child('typing')
  }

  /** Show "typing..." indicator to a user. */
  async startTyping(baseUrl: string, token: string, userId: string): Promise<void> {
    const ticket = await this.getTicket(baseUrl, token, userId)
    if (!ticket) return
    await this.api.sendTyping(baseUrl, token, userId, ticket, 1)
    this.logger.debug(`Started typing for ${userId}`)
  }

  /** Cancel "typing..." indicator. */
  async stopTyping(baseUrl: string, token: string, userId: string): Promise<void> {
    const ticket = await this.getTicket(baseUrl, token, userId)
    if (!ticket) return
    await this.api.sendTyping(baseUrl, token, userId, ticket, 2)
    this.logger.debug(`Stopped typing for ${userId}`)
  }

  /** Clear ticket cache (e.g. on re-login). */
  clearCache(): void {
    this.ticketCache.clear()
  }

  private async getTicket(
    baseUrl: string,
    token: string,
    userId: string,
  ): Promise<string | undefined> {
    // Check cache
    const now = Date.now()
    const cached = this.ticketCache.get(userId)
    if (cached && now < cached.expiresAt) {
      return cached.ticket
    }

    // Need context_token to get ticket
    const contextToken = this.contextStore.get(userId)
    if (!contextToken) {
      this.logger.debug(`No context token for ${userId}, cannot get typing ticket`)
      return undefined
    }

    try {
      const config = await this.api.getConfig(baseUrl, token, userId, contextToken)
      if (!config.typing_ticket) {
        this.logger.debug('getconfig returned no typing_ticket')
        return undefined
      }

      // Cache the ticket
      this.ticketCache.set(userId, {
        ticket: config.typing_ticket,
        expiresAt: now + TypingService.TICKET_TTL_MS,
      })

      return config.typing_ticket
    } catch (error) {
      this.logger.warn(`Failed to get typing ticket: ${error instanceof Error ? error.message : String(error)}`)
      // Remove stale cache entry on failure
      this.ticketCache.delete(userId)
      return undefined
    }
  }
}
