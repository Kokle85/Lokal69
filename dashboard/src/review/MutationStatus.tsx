import { useEffect, useRef, type ReactNode } from 'react'
import { describeError } from '../api/errors'
import { ErrorPanel } from '../components/ui'
import type { MutationPhase } from './useIdempotentMutation'

/**
 * Announces a mutation's state. "Saved" is shown ONLY for a server-confirmed result; a lost
 * response is shown as "not confirmed" with a safe same-key retry.
 */
export function MutationStatus<B, R>({
  phase,
  onRetry,
  onDiscard,
  confirmed,
  pendingText = 'Sending…',
  extraOnRejected,
}: {
  phase: MutationPhase<B, R>
  onRetry: () => void
  onDiscard?: () => void
  confirmed: (data: R) => ReactNode
  pendingText?: string
  extraOnRejected?: ReactNode
}) {
  const region = useRef<HTMLDivElement>(null)
  useEffect(() => {
    // Keyboard users: when the control that started the mutation is gone or disabled by the result
    // (e.g. "Submit decision" after a confirmed decision), keep focus on the result, not the page top.
    if (phase.kind === 'idle' || phase.kind === 'pending') return
    const target = region.current
    const active = document.activeElement as (HTMLElement & { disabled?: boolean }) | null
    const nowhere = !active || active === document.body || active.disabled === true || !active.isConnected
    // A focused container around the result (e.g. <main> after a click on the disabled button).
    const container = active !== null && target !== null && active !== target && active.contains(target)
    if (target && (nowhere || container)) target.focus()
  }, [phase])
  return (
    <div className="mutation-status" aria-live="polite" role="status" ref={region} tabIndex={-1}>
      {phase.kind === 'pending' ? (
        <p className="pending" data-testid="mutation-pending">
          {pendingText} Not saved yet: waiting for the server to confirm.
        </p>
      ) : null}
      {phase.kind === 'confirmed' ? (
        <div className="notice notice-ok" data-testid="mutation-confirmed">
          {confirmed(phase.envelope.data)}{' '}
          <span className="muted">
            {phase.viaRetry ? '(confirmed by the server on an idempotent retry)' : '(confirmed by the server)'}
          </span>
        </div>
      ) : null}
      {phase.kind === 'unconfirmed' ? (
        <div className="notice notice-warn" data-testid="mutation-unconfirmed">
          <p className="panel-title">Not confirmed: it may or may not have been saved</p>
          <p>{describeError(phase.error).message}</p>
          {phase.error.code === 'RATE_LIMITED' || phase.error.code === 'FORBIDDEN' || phase.error.code === 'UNAUTHENTICATED' ? (
            <p>The last retry was refused before the server looked at it ({describeError(phase.error).title.toLowerCase()}), so the earlier send is still unconfirmed.{describeError(phase.error).hint ? ` ${describeError(phase.error).hint}` : ''}</p>
          ) : null}
          <p>
            Retrying sends the identical request with the same idempotency key, so the server applies it at most once and
            returns the original result.
          </p>
          <div className="form-actions">
            <button type="button" className="button" onClick={onRetry}>
              Retry the same request
            </button>
            {onDiscard ? (
              <button type="button" className="button secondary" onClick={onDiscard}>
                Discard and reload
              </button>
            ) : null}
          </div>
          {phase.error.correlationId ? (
            <p className="muted">
              Reference <code>{phase.error.correlationId}</code>
            </p>
          ) : null}
        </div>
      ) : null}
      {phase.kind === 'rejected' ? (
        <div data-testid="mutation-rejected">
          <ErrorPanel error={phase.error} />
          {extraOnRejected}
        </div>
      ) : null}
    </div>
  )
}
