import type { IncomingMessage } from '../message/types.js'

/**
 * Context object passed through the middleware chain.
 * Middleware can attach arbitrary data for downstream handlers.
 */
export interface MessageContext {
  /** The parsed incoming message. */
  message: IncomingMessage
  /** Arbitrary state bag for middleware to share data. */
  state: Map<string, unknown>
  /** Whether this message has been handled (stops further processing). */
  handled: boolean
}

/**
 * A next() function that invokes the next middleware in the chain.
 */
export type NextFunction = () => Promise<void>

/**
 * Middleware function signature.
 * Call next() to pass to the next middleware, or skip it to short-circuit.
 */
export type Middleware = (ctx: MessageContext, next: NextFunction) => Promise<void> | void
