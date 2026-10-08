/**
 * One idempotent mutation with an honest client-side state machine.
 *
 *   idle -> pending -> confirmed            (server answered 2xx: the ONLY "saved" state)
 *                   -> rejected             (server refused, e.g. a conflict: nothing was saved)
 *                   -> unconfirmed          (network loss / 5xx after sending: outcome unknown)
 *   unconfirmed -> pending (retry)          (SAME idempotency key and SAME body)
 *   retry refused before evaluation         (401/403/429: stays unconfirmed, see NOT_EVALUATED_CODES)
 *   retry turned away as busy               (a `retryable` refusal, e.g. 409 VERSION_CONFLICT with
 *                                            `retryable: true`: stays unconfirmed, see stillUnknown)
 *
 * Guarantees:
 * - at most one request is in flight (a second click while pending is ignored, guarded by a ref so
 *   two clicks in the same event loop tick cannot both send);
 * - the idempotency key is generated once per logical attempt and reused verbatim on retry, so the
 *   server returns the original result instead of applying the change twice;
 * - while unconfirmed, a different body can NOT be sent with a new key (the caller must retry the
 *   same request or discard it after checking the server state), so an unknown outcome can never
 *   turn into two writes.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { newIdempotencyKey, type ApiResponse } from '../api/client'
import { ApiError, isApiError } from '../api/errors'
import type { ResponseEnvelope } from '../api/types'

export interface MutationAttempt<B> {
  key: string
  body: B
  /** 1 for the first send, +1 per retry of the same attempt. */
  sends: number
}

export type MutationPhase<B, R> =
  | { kind: 'idle' }
  | { kind: 'pending'; attempt: MutationAttempt<B> }
  | { kind: 'confirmed'; attempt: MutationAttempt<B>; envelope: ResponseEnvelope<R>; viaRetry: boolean }
  | { kind: 'rejected'; attempt: MutationAttempt<B>; error: ApiError }
  | { kind: 'unconfirmed'; attempt: MutationAttempt<B>; error: ApiError }

export interface MutationHooks<B, R> {
  /** Called right before each send (first send and retries). */
  beforeSend?(attempt: MutationAttempt<B>): void
  /** Called when an attempt reaches a definitive state (confirmed or rejected). */
  settled?(attempt: MutationAttempt<B>, outcome: { envelope: ResponseEnvelope<R> } | { error: ApiError }): void
}

export interface IdempotentMutation<B, R> {
  phase: MutationPhase<B, R>
  /** Start a NEW attempt (new key). Ignored while pending; refused while unconfirmed. */
  submit(input: Omit<B, 'idempotency_key'>): Promise<void>
  /** Re-send the unconfirmed attempt with the same key and body. */
  retry(): Promise<void>
  /** Forget the current attempt (after the caller checked the server state). */
  reset(): void
}

function toApiError(error: unknown): ApiError {
  return isApiError(error)
    ? error
    : new ApiError({ code: 'BAD_RESPONSE', message: String(error), status: null, retryable: true, outcomeUnknown: true })
}

/**
 * Refusals that happen BEFORE the server looks at the request (authentication, membership/scope,
 * rate limiting, a cancelled send). On the first send they are definitive (nothing was applied),
 * but on a RETRY of an unconfirmed attempt they say nothing about whether the ORIGINAL send was
 * applied: the attempt stays unconfirmed, so it can only be retried with the same key (never
 * replaced by a new attempt with a new key, which could apply the change twice).
 */
const NOT_EVALUATED_CODES = new Set<ApiError['code']>(['UNAUTHENTICATED', 'FORBIDDEN', 'RATE_LIMITED', 'ABORTED'])

/**
 * Whether an attempt's outcome is still unknown after `error`.
 *
 * A RETRY that the server turns away as `retryable` (a transient refusal: `409 VERSION_CONFLICT`
 * with `retryable: true` after a lock timeout, a serialization failure or a deadlock, or while the
 * same idempotency key is still in progress) proves only that THIS retry was rolled back; the
 * original send may still commit. Settling it as "rejected" would unlock a new attempt with a new key
 * (a second note, recheck or re-qualifying resume once the original commits) and report "nothing was
 * saved" for a change that may apply, so the attempt stays unconfirmed: only another same-key retry
 * (or the owner's explicit discard after checking the server state) ends it. On a FIRST send a
 * retryable refusal is definitive: that transaction was rolled back and nothing else is in flight.
 */
function stillUnknown(error: ApiError, viaRetry: boolean): boolean {
  if (error.outcomeUnknown) return true
  return viaRetry && (NOT_EVALUATED_CODES.has(error.code) || error.retryable)
}

export function useIdempotentMutation<B extends { idempotency_key: string }, R>(
  operation: string,
  send: (body: B) => Promise<ApiResponse<R>>,
  hooks: MutationHooks<B, R> = {},
): IdempotentMutation<B, R> {
  const [phase, setPhaseState] = useState<MutationPhase<B, R>>({ kind: 'idle' })
  const phaseRef = useRef(phase)
  const inFlight = useRef(false)
  const sendRef = useRef(send)
  const hooksRef = useRef(hooks)
  useEffect(() => {
    // Sends are triggered by user events after commit, so the latest callbacks are always in place.
    sendRef.current = send
    hooksRef.current = hooks
  })

  const setPhase = useCallback((next: MutationPhase<B, R>) => {
    phaseRef.current = next
    setPhaseState(next)
  }, [])

  const run = useCallback(
    async (attempt: MutationAttempt<B>, viaRetry: boolean) => {
      inFlight.current = true
      setPhase({ kind: 'pending', attempt })
      hooksRef.current.beforeSend?.(attempt)
      try {
        const { envelope } = await sendRef.current(attempt.body)
        setPhase({ kind: 'confirmed', attempt, envelope, viaRetry })
        hooksRef.current.settled?.(attempt, { envelope })
      } catch (error) {
        const apiError = toApiError(error)
        if (stillUnknown(apiError, viaRetry)) {
          setPhase({ kind: 'unconfirmed', attempt, error: apiError })
        } else {
          setPhase({ kind: 'rejected', attempt, error: apiError })
          hooksRef.current.settled?.(attempt, { error: apiError })
        }
      } finally {
        inFlight.current = false
      }
    },
    [setPhase],
  )

  const submit = useCallback(
    async (input: Omit<B, 'idempotency_key'>) => {
      if (inFlight.current) return
      if (phaseRef.current.kind === 'unconfirmed') return
      const attempt: MutationAttempt<B> = {
        key: newIdempotencyKey(operation),
        body: { ...input, idempotency_key: '' } as B,
        sends: 1,
      }
      attempt.body = { ...attempt.body, idempotency_key: attempt.key }
      await run(attempt, false)
    },
    [operation, run],
  )

  const retry = useCallback(async () => {
    if (inFlight.current) return
    const current = phaseRef.current
    if (current.kind !== 'unconfirmed') return
    await run({ ...current.attempt, sends: current.attempt.sends + 1 }, true)
  }, [run])

  const reset = useCallback(() => {
    if (inFlight.current) return
    setPhase({ kind: 'idle' })
  }, [setPhase])

  return { phase, submit, retry, reset }
}
