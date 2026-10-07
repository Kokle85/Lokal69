/**
 * Shared presentational components.
 *
 * Security: every value from the API (and especially seller-provided text) is rendered as a React
 * text child, so it is escaped by React. Nothing here (or anywhere in the app) injects raw HTML,
 * and links are rendered only for absolute http(s) URLs (`safeHttpUrl`), always opening in a new
 * tab with `rel="noopener noreferrer"` so the external page cannot reach `window.opener`.
 */
import { useEffect, useState, type ReactNode } from 'react'
import { describeError, type ApiError } from '../api/errors'
import type { AmountView, ResponseWarning } from '../api/types'
import { ageText, amountText, dateTimeText, label, safeHttpUrl } from '../format'

// ----------------------------------------------------------------------------------- money

export function Amount({ value, showReason = false }: { value: AmountView | null | undefined; showReason?: boolean }) {
  const text = amountText(value)
  if (value?.status === 'known' && value.amount !== null) {
    return <span className="amount">{text}</span>
  }
  const kind = value?.status === 'not_applicable' ? 'not-applicable' : 'unknown'
  return (
    <span className={`amount amount-${kind}`} data-amount-status={value?.status ?? 'unknown'}>
      {text}
      {showReason && value?.reason ? <span className="amount-reason"> ({value.reason})</span> : null}
    </span>
  )
}

// ----------------------------------------------------------------------------------- time

export function Timestamp({ value, timeZone, withAge = false }: { value: string | null | undefined; timeZone: string; withAge?: boolean }) {
  if (!value) return <span className="muted">never</span>
  return (
    <time dateTime={value} title={value}>
      {dateTimeText(value, timeZone)}
      {withAge ? <span className="muted"> ({ageText(value)})</span> : null}
    </time>
  )
}

// ----------------------------------------------------------------------------------- badges

const BADGE_TONES: Record<string, string> = {
  running: 'ok',
  active: 'ok',
  healthy: 'ok',
  eligible_primary: 'ok',
  eligible_manual_profile: 'ok',
  available: 'ok',
  live_smoke_passed: 'ok',
  quote_supported: 'ok',
  estimated: 'info',
  shortlisted: 'ok',
  watch: 'info',
  pending: 'info',
  claimed: 'info',
  needs_information: 'warn',
  needs_facts: 'warn',
  incomplete: 'warn',
  paused: 'warn',
  degraded: 'warn',
  stale: 'warn',
  not_scanned: 'warn',
  not_started: 'warn',
  unknown: 'warn',
  PROPOSED: 'warn',
  DISABLED: 'muted',
  disabled: 'muted',
  rejected: 'bad',
  blocked: 'bad',
  access_blocked: 'bad',
  parser_unhealthy: 'bad',
  unhealthy: 'bad',
  invalid: 'bad',
  dead_letter: 'bad',
  uncertain: 'warn',
  removed: 'bad',
  sold_claimed: 'bad',
}

export function Badge({ value, tone, children }: { value?: string | null; tone?: string; children?: ReactNode }) {
  const resolved = tone ?? (value ? BADGE_TONES[value] : undefined) ?? 'neutral'
  return <span className={`badge badge-${resolved}`}>{children ?? label(value)}</span>
}

// ----------------------------------------------------------------------------------- text

/** Seller-provided or other untrusted text: escaped by React, whitespace preserved, visibly labelled. */
export function UntrustedText({ text, as = 'span' }: { text: string | null | undefined; as?: 'span' | 'p' | 'blockquote' }) {
  if (text === null || text === undefined || text === '') return <span className="muted">not stated</span>
  if (as === 'blockquote') return <blockquote className="untrusted">{text}</blockquote>
  if (as === 'p') return <p className="untrusted">{text}</p>
  return <span className="untrusted">{text}</span>
}

// ----------------------------------------------------------------------------------- links

export function ExternalLink({ href, children }: { href: string | null | undefined; children: ReactNode }) {
  const safe = safeHttpUrl(href)
  if (!safe) {
    return (
      <span className="link-withheld">
        {children} <span className="muted">(link withheld: not an http(s) address)</span>
      </span>
    )
  }
  return (
    <a href={safe} target="_blank" rel="noopener noreferrer" referrerPolicy="no-referrer" className="external-link">
      {children}
      <span className="sr-only"> (opens an external site in a new tab)</span>
      <span aria-hidden="true"> ↗</span>
    </a>
  )
}

// ----------------------------------------------------------------------------------- states

export function LoadingState({ label: text = 'Loading' }: { label?: string }) {
  return (
    <div className="state state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      {text}…
    </div>
  )
}

export function EmptyState({ children }: { children: ReactNode }) {
  return <div className="state state-empty">{children}</div>
}

export function ErrorPanel({ error, onRetry, retryLabel = 'Retry' }: { error: ApiError; onRetry?: () => void; retryLabel?: string }) {
  const description = describeError(error)
  return (
    <div className="panel panel-error" role="alert">
      <p className="panel-title">{description.title}</p>
      <p>{description.message}</p>
      {error.message && error.message !== description.message && error.code !== 'NETWORK_ERROR' ? (
        <p className="muted">Server message: {error.message}</p>
      ) : null}
      {description.hint ? <p>{description.hint}</p> : null}
      <p className="muted">
        Error code <code>{error.code}</code>
        {error.correlationId ? (
          <>
            {' '}
            · reference (correlation id) <code data-testid="correlation-id">{error.correlationId}</code>
          </>
        ) : null}
      </p>
      {onRetry ? (
        <button type="button" className="button secondary" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  )
}

export function Warnings({ warnings }: { warnings: ResponseWarning[] | null | undefined }) {
  if (!warnings?.length) return null
  return (
    <ul className="warnings" aria-label="Warnings from the server">
      {warnings.map((item, index) => (
        <li key={`${item.code}-${index}`} className="warning">
          <span className="warning-code">{label(item.code).toLowerCase()}</span> {item.message}
        </li>
      ))}
    </ul>
  )
}

export const STALE_AFTER_MS = 5 * 60 * 1000

/** Re-renders periodically so age-based warnings appear without user action. */
export function useNow(intervalMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}

/** "As of" line plus a stale-view warning with a reload path. */
export function ViewMeta({
  asOf,
  fetchedAt,
  timeZone,
  onReload,
  reloading,
}: {
  asOf: string | null | undefined
  fetchedAt: number | null
  timeZone: string
  onReload: () => void
  reloading: boolean
}) {
  const now = useNow()
  const stale = fetchedAt !== null && now - fetchedAt > STALE_AFTER_MS
  return (
    <div className="view-meta">
      <span className="muted">
        Data as of <Timestamp value={asOf ?? null} timeZone={timeZone} />
      </span>
      <button type="button" className="button small secondary" onClick={onReload} disabled={reloading}>
        {reloading ? 'Reloading…' : 'Reload'}
      </button>
      {stale ? (
        <p className="stale-warning" role="status">
          This view was loaded more than {Math.round(STALE_AFTER_MS / 60000)} minutes ago and may be stale. Reload before
          acting on it.
        </p>
      ) : null}
    </div>
  )
}

export function Section({ title, children, id, actions }: { title: string; children: ReactNode; id?: string; actions?: ReactNode }) {
  const headingId = id ? `${id}-heading` : undefined
  return (
    <section className="section" aria-labelledby={headingId} id={id}>
      <div className="section-header">
        <h2 id={headingId}>{title}</h2>
        {actions}
      </div>
      {children}
    </section>
  )
}

export function KeyValues({ items }: { items: Array<[string, ReactNode]> }) {
  return (
    <dl className="key-values">
      {items.map(([key, value]) => (
        <div key={key} className="key-value">
          <dt>{key}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export function Notice({ tone = 'info', children }: { tone?: 'info' | 'warn' | 'bad' | 'ok'; children: ReactNode }) {
  return <div className={`notice notice-${tone}`}>{children}</div>
}
