/**
 * One idempotent mutation with an honest client-side state machine.
 *
 *   idle -> pending -> confirmed            (server answered 2xx: the ONLY "saved" state)
 *                   -> rejected             (server refused, e.g. a conflict: nothing was saved)
 *                   -> unconfirmed          (network loss / 5xx after sending: outcome unknown)
 *   unconfirmed -> pending (retry)          (SAME idempotency key and SAME body)
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
        if (apiError.outcomeUnknown) {
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
