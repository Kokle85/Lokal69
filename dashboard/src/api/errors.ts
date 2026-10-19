import { ERROR_CODES, type ApiErrorResponse, type ErrorCode, type ErrorDetailValue } from './types'

/** Client-side failure kinds that never come from the server. */
export type ClientErrorCode = 'NETWORK_ERROR' | 'BAD_RESPONSE' | 'ABORTED'

export interface ApiErrorInit {
  code: ErrorCode | ClientErrorCode
  message: string
  status: number | null
  retryable: boolean
  retryAfterSeconds?: number | null
  correlationId?: string | null
  details?: Record<string, ErrorDetailValue> | null
  /**
   * True for a mutation whose outcome is unknown: the request may have reached the server and been
   * committed (network loss after the write, gateway failure, 5xx). Only an idempotent retry with
   * the SAME idempotency key and body can resolve it.
   */
  outcomeUnknown?: boolean
}

export class ApiError extends Error {
  readonly code: ErrorCode | ClientErrorCode
  readonly status: number | null
  readonly retryable: boolean
  readonly retryAfterSeconds: number | null
  readonly correlationId: string | null
  readonly details: Record<string, ErrorDetailValue> | null
  readonly outcomeUnknown: boolean

  constructor(init: ApiErrorInit) {
    super(init.message)
    this.name = 'ApiError'
    this.code = init.code
    this.status = init.status
    this.retryable = init.retryable
    this.retryAfterSeconds = init.retryAfterSeconds ?? null
    this.correlationId = init.correlationId ?? null
    this.details = init.details ?? null
    this.outcomeUnknown = init.outcomeUnknown ?? false
  }

  /** A server-side detail field list (`details.fields`), never values. */
  get fields(): string[] {
    const fields = this.details?.fields
    return Array.isArray(fields) ? fields : []
  }

  get isConflict(): boolean {
    return (
      this.code === 'VERSION_CONFLICT' ||
      this.code === 'ALREADY_CLAIMED' ||
      this.code === 'CLAIM_EXPIRED' ||
      this.code === 'IDEMPOTENCY_CONFLICT'
    )
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

const CORRELATION_ID = /^[\x21-\x7e]{1,200}$/

function isErrorCode(value: unknown): value is ErrorCode {
  return typeof value === 'string' && (ERROR_CODES as readonly string[]).includes(value)
}

/** Parse an `ApiErrorResponse` body defensively; `null` when the body is not one. */
export function parseErrorBody(body: unknown): ApiErrorResponse['error'] | null {
  if (typeof body !== 'object' || body === null) return null
  const error = (body as { error?: unknown }).error
  if (typeof error !== 'object' || error === null) return null
  const candidate = error as Record<string, unknown>
  if (!isErrorCode(candidate.code) || typeof candidate.message !== 'string') return null
  const details =
    typeof candidate.details === 'object' && candidate.details !== null && !Array.isArray(candidate.details)
      ? (candidate.details as Record<string, ErrorDetailValue>)
      : null
  return {
    code: candidate.code,
    message: candidate.message.slice(0, 500),
    retryable: candidate.retryable === true,
    retry_after_seconds:
      typeof candidate.retry_after_seconds === 'number' && Number.isFinite(candidate.retry_after_seconds)
        ? candidate.retry_after_seconds
        : null,
    correlation_id:
      typeof candidate.correlation_id === 'string' && CORRELATION_ID.test(candidate.correlation_id)
        ? candidate.correlation_id
        : null,
    details,
  }
}

export interface ErrorDescription {
  title: string
  message: string
  /** What the user can do next, if anything. */
  hint: string | null
}

export interface DescribeOptions {
  /**
   * What a `VERSION_CONFLICT` is about (default: a review case or listing), e.g. "inquiry controls",
   * so the title never names the wrong object.
   */
  subject?: string
}

/** A stable machine reason (`details.reason`, e.g. `inquiry_kill_switch`), or `null`. */
export function guardReason(error: ApiError): string | null {
  const reason = error.details?.reason
  return typeof reason === 'string' && /^[a-z][a-z0-9_]{0,79}$/.test(reason) ? reason : null
}

/**
 * The typed reason of a TRANSIENT conflict (`errors_map.TransientConflict`, contract section 3):
 * `busy` (lock timeout, serialization failure, deadlock or a lost race: this request was rolled
 * back) or `in_progress` (a request with the SAME idempotency key is still running: its outcome is
 * unknown). `null` for any other error.
 */
export type TransientReason = 'busy' | 'in_progress'

export function transientReason(error: ApiError): TransientReason | null {
  if (error.code !== 'VERSION_CONFLICT') return null
  const reason = guardReason(error)
  return reason === 'busy' || reason === 'in_progress' ? reason : null
}

/**
 * Whether a refusal is transient (retry later): the typed `details.reason` first, then the
 * server's `retryable` flag as the fallback for an answer without a reason.
 */
export function isTransient(error: ApiError): boolean {
  return transientReason(error) !== null || error.retryable
}

/** `409 VERSION_CONFLICT`, `details.reason = suppressions_changed` (inquiry resume, contract 10.2). */
export const SUPPRESSIONS_CHANGED = 'suppressions_changed'

function countDetail(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null
}

/** The counts of a `suppressions_changed` refusal (`null` for any other error). */
export function suppressionsChanged(error: ApiError): { expected: number | null; current: number | null } | null {
  if (error.code !== 'VERSION_CONFLICT' || guardReason(error) !== SUPPRESSIONS_CHANGED) return null
  return {
    expected: countDetail(error.details?.expected_removable_suppressions),
    current: countDetail(error.details?.current_removable_suppressions),
  }
}

/**
 * User-facing wording per error code. The server message is shown too (it is safe by contract),
 * but the title/hint are fixed strings so the UI never depends on server wording.
 */
export function describeError(error: ApiError, options: DescribeOptions = {}): ErrorDescription {
  switch (error.code) {
    case 'ALREADY_CLAIMED':
      return {
        title: 'Already claimed by another reviewer',
        message: 'Another reviewer holds an active claim on this case. Nothing was saved.',
        hint: 'Reload the case later; the claim is released when they finish or it expires.',
      }
    case 'CLAIM_EXPIRED':
      return {
        title: 'Your claim expired or is no longer current',
        message:
          'The claim handle is not the current one (it expired, was released, or the case was claimed again elsewhere). Nothing was saved.',
        hint: 'Reload the case and claim it again; your draft is kept.',
      }
    case 'VERSION_CONFLICT': {
      // `errors_map.TransientConflict`: the typed `details.reason` decides; `retryable` without a
      // reason is the fallback (an older answer).
      const transient = transientReason(error)
      if (transient === 'in_progress') {
        return {
          title: 'The same request is still being processed',
          message:
            'The server is still processing an identical request with the same key. Its outcome is not known yet: it may still be applied.',
          hint: 'Retry the same request in a moment; the server applies it at most once.',
        }
      }
      if (transient === 'busy' || error.retryable) {
        // A lock timeout, serialization failure, deadlock or lost race: this request was rolled back.
        return {
          title: 'The server was busy',
          message:
            transient === 'busy'
              ? `The ${options.subject ?? 'record'} was busy (another operation held it). This request was not applied.`
              : `The ${options.subject ?? 'record'} was busy (another operation held it, or the same request was still being processed). This request was not applied.`,
          hint: 'Try again in a moment.',
        }
      }
      const changed = suppressionsChanged(error)
      if (changed) {
        const saw = changed.expected === null ? 'the count you saw' : `${changed.expected}`
        const now = changed.current === null ? 'a different number' : `${changed.current}`
        return {
          title: 'The suppressions a resume would remove changed',
          message: `You confirmed removing ${saw} suppression(s), but the server now counts ${now}. The whole resume was refused: nothing was resumed and no suppression was removed.`,
          hint: 'The controls are reloaded: review the current count and confirm again.',
        }
      }
      if (options.subject) {
        return {
          title: `The ${options.subject} changed`,
          message: `The ${options.subject} changed since you loaded them${conflictSuffix(error, 'version')}. Nothing was saved.`,
          hint: 'Reload to see the current state, then decide again.',
        }
      }
      return {
        title: 'The case or listing changed',
        message: `The data changed since you loaded it${conflictSuffix(error, 'case version')}. Nothing was saved.`,
        hint: 'Reload to see the current facts before deciding; your draft is kept.',
      }
    }
    case 'EMAIL_DELIVERY_UNCERTAIN':
      return {
        title: 'Seller e-mail outcome uncertain',
        message:
          'An earlier send attempt may have reached the provider. It is held for reconciliation with positive evidence and is never resent blindly.',
        hint: null,
      }
    case 'IDEMPOTENCY_CONFLICT':
      return {
        title: 'Request key already used',
        message: 'This request key was already used for a different request. Nothing new was saved.',
        hint: 'Reload and submit again.',
      }
    case 'UNAUTHENTICATED':
      return {
        title: 'Signed out',
        message: 'Your session is missing, expired or was revoked.',
        hint: 'Sign in again.',
      }
    case 'FORBIDDEN':
      return {
        title: 'Not permitted',
        message: 'Your role in this workspace does not allow this.',
        hint: null,
      }
    case 'NOT_FOUND':
      return { title: 'Not found', message: 'This item does not exist in the selected workspace.', hint: null }
    case 'VALIDATION_ERROR':
      return {
        title: 'Check the input',
        message: error.fields.length
          ? `The server refused these fields: ${error.fields.join(', ')}.`
          : error.message,
        hint: error.details?.cursor ? 'The page cursor expired or no longer matches; restart from the first page.' : null,
      }
    case 'SOURCE_PAUSED':
      return { title: 'Source paused', message: 'The source is paused or not enabled.', hint: null }
    case 'ACCESS_BLOCKED':
      return { title: 'Source access blocked', message: 'The source reported an access block.', hint: null }
    case 'RATE_LIMITED':
      return {
        title: 'Too many requests',
        message: 'The server asked to slow down.',
        hint:
          error.retryAfterSeconds !== null
            ? `Try again in about ${error.retryAfterSeconds} seconds.`
            : 'Try again shortly.',
      }
    case 'INSUFFICIENT_DATA':
      return { title: 'Not enough data', message: error.message, hint: null }
    case 'DEPENDENCY_UNAVAILABLE':
      return {
        title: 'Service temporarily unavailable',
        message: 'A backend dependency is unavailable.',
        hint: 'Try again shortly.',
      }
    case 'INTERNAL_ERROR':
      return {
        title: 'Unexpected server error',
        message: 'The server failed unexpectedly.',
        hint: 'Try again; quote the reference below if it persists.',
      }
    case 'NETWORK_ERROR':
      return {
        title: 'Network problem',
        message: error.outcomeUnknown
          ? 'The connection failed after the request was sent. The server may or may not have saved it.'
          : 'The server could not be reached.',
        hint: error.outcomeUnknown ? 'Retry the same request; a retry is safe and cannot save it twice.' : 'Check the connection and retry.',
      }
    case 'BAD_RESPONSE':
      return {
        title: 'Unexpected response',
        message: error.outcomeUnknown
          ? 'The server answered unexpectedly after the request was sent. It may or may not have been saved.'
          : 'The server answered with an unexpected response.',
        hint: 'Retry; a retry of the same request is safe.',
      }
    case 'ABORTED':
      return { title: 'Cancelled', message: 'The request was cancelled.', hint: null }
  }
}

function conflictSuffix(error: ApiError, versionLabel: string): string {
  const details = error.details ?? {}
  const parts: string[] = []
  if (typeof details.current_listing_revision === 'number') {
    parts.push(`listing revision ${details.current_listing_revision} is now current`)
  }
  if (typeof details.current_version === 'number') {
    parts.push(`${versionLabel} ${details.current_version} is now current`)
  }
  return parts.length ? ` (${parts.join('; ')})` : ''
}
