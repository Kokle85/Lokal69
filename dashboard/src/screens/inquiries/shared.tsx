/**
 * Shared pieces of the spec v1.1 screens (seller inquiries, replies, inquiry control, mail workers,
 * lags, evaluation).
 *
 * Standing authorization (spec 37.1): there is NO approve, send or reply control anywhere in these
 * screens. The original-language message and its Macedonian preview are informational audit views;
 * the pipeline sends from validated domain records only.
 */
import type { ReactNode } from 'react'
import { NavLink } from 'react-router'
import type {
  ContactStatus,
  InquiryState,
  InquiryVehicleRef,
  LagView,
  ReplySummaryView,
  RequestKind,
  Scope,
  SuppressionReason,
} from '../../api/types'
import { Badge, Notice } from '../../components/ui'
import { durationText, label } from '../../format'
import { useWorkspace } from '../../workspace/WorkspaceProvider'

const AREA_LINKS: Array<{ to: string; text: string; end?: boolean }> = [
  { to: '/inquiries', text: 'Inquiries', end: true },
  { to: '/replies', text: 'Replies', end: true },
  { to: '/inquiry-control', text: 'Inquiry control' },
  { to: '/mail-workers', text: 'Mail workers' },
  { to: '/evaluation', text: '15-day evaluation' },
]

/** Secondary navigation of the seller-inquiry area. */
export function InquiryAreaNav() {
  return (
    <nav aria-label="Seller inquiry area" className="subnav">
      <ul>
        {AREA_LINKS.map((item) => (
          <li key={item.to}>
            <NavLink to={item.to} end={item.end ?? false} className={({ isActive }) => (isActive ? 'active' : undefined)}>
              {item.text}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  )
}

/** Renders `children` only for a principal holding `scope`; otherwise explains the role limit. */
export function RequireScope({ scope, what, children }: { scope: Scope; what: string; children: ReactNode }) {
  const { can, role } = useWorkspace()
  if (can(scope)) return <>{children}</>
  return (
    <div className="screen">
      <h1>Not available for your role</h1>
      <Notice tone="info">
        Your role ({role}) cannot view {what}: it needs the <code>{scope}</code> permission. Nothing here is hidden from the
        owner; ask the owner if you need access.
      </Notice>
    </div>
  )
}

/** The standing-authorization statement shown on the inquiry screens (spec 37.1). */
export function StandingAuthorizationNote() {
  return (
    <p className="muted small" data-testid="standing-authorization">
      Bounded standing authorization: one automatic initial inquiry per verified vehicle/seller pair (availability, documents,
      lowest price). There is no per-message approval and no manual send action here; nothing is sent unless the mode is
      automatic, the kill switch is off, the standing authorization is active and the sender is verified.
    </p>
  )
}

/** A short, readable reference to an inquiry's vehicle (listing reference and source). */
export function vehicleText(vehicle: InquiryVehicleRef): string {
  const reference = vehicle.listing_reference ?? `listing ${vehicle.listing_id.slice(0, 8)}`
  return vehicle.source_key ? `${reference} (${vehicle.source_key})` : reference
}

const STATE_HELP: Record<InquiryState, string> = {
  candidate: 'identified; readiness not decided yet',
  qualifying: 'qualified; reserved automatically once caps, cooldown and pause allow',
  reserved: 'reserved (one inquiry per vehicle/seller pair, quota debited)',
  queued: 'queued for the send route',
  sending: 'a send attempt is in progress',
  accepted: 'accepted by the provider (not proof of delivery or reading)',
  held_facts: 'held: facts or technical checks missing (no approval is being waited for)',
  uncertain: 'outcome uncertain: held for reconciliation, never resent blindly',
  suppressed: 'suppressed: will not be sent',
  failed_definite: 'failed definitively',
  cancelled: 'cancelled before transmission',
  replied: 'the seller replied',
  bounced: 'bounced',
  seller_opted_out: 'the seller asked not to be contacted',
  no_reply_yet: 'no reply yet',
}

export function stateHelp(state: InquiryState): string {
  return STATE_HELP[state]
}

export function InquiryStateBadge({ state }: { state: InquiryState }) {
  return (
    <span title={STATE_HELP[state]}>
      <Badge value={state} />
    </span>
  )
}

const RECIPIENT_TEXT: Record<ContactStatus, string> = {
  verified: 'verified',
  unverified: 'not verified',
  unavailable: 'no e-mail on the listing',
  changed: 'changed since verification',
  unknown: 'unknown',
}

export function recipientText(status: ContactStatus): string {
  return RECIPIENT_TEXT[status]
}

const SUPPRESSION_TEXT: Record<SuppressionReason, string> = {
  hard_bounce: 'hard bounce',
  complaint: 'complaint',
  seller_opt_out: 'seller opted out',
  source_paused: 'source paused',
  sender_revoked: 'sender access revoked',
  unresolved_send_outcome: 'unresolved send outcome',
  kill_switch: 'kill switch',
  contradictory_availability: 'contradictory availability',
  manual: 'manual (owner)',
  authorization_revoked: 'standing authorization revoked',
}

export function suppressionText(reason: SuppressionReason | null): string {
  return reason ? SUPPRESSION_TEXT[reason] : 'none'
}

const REQUEST_TEXT: Record<RequestKind, string> = {
  payment: 'asks for a payment or deposit',
  reservation: 'proposes a reservation',
  identity_document: 'asks for identity documents',
  appointment: 'proposes an appointment or viewing',
  commitment: 'asks for a commitment',
  price_acceptance: 'asks you to accept a price',
  opt_out: 'asks not to be contacted again',
  complaint: 'complains about the contact',
}

export function requestText(kind: RequestKind): string {
  return REQUEST_TEXT[kind]
}

/**
 * The availability a reply row states. A quarantined reply is an unverified possible match that may
 * be unrelated personal mail, so its text is the owner's only (`config:admin`, the server's
 * `views.inquiries.reply_content_visible` rule): a claim DERIVED from that text is withheld from
 * everyone else, and the owner sees it labelled as an unverified match, never as a stated fact.
 */
export function ReplyAvailability({ reply }: { reply: ReplySummaryView }) {
  const { can } = useWorkspace()
  if (reply.quarantined && !can('config:admin')) {
    return (
      <span className="muted" data-testid="availability-withheld">
        withheld (unverified match)
      </span>
    )
  }
  if (!reply.availability) return <span className="muted">not extracted</span>
  return (
    <>
      {label(reply.availability)}
      {reply.quarantined ? ' (unverified match)' : ''}
    </>
  )
}

/**
 * One lag: a measured value, or `unknown` / `inconsistent` with the reason (never zero). A
 * configured interval is shown as context only, never as an observed latency.
 */
export function LagValue({ lag }: { lag: LagView }) {
  return (
    <span className="lag" data-testid={`lag-${lag.name}`} data-lag-status={lag.status}>
      {lag.status === 'measured' && lag.value_seconds !== null ? (
        <strong>{durationText(lag.value_seconds)}</strong>
      ) : (
        <span className="status-unknown">{lag.status}</span>
      )}
      {lag.status !== 'measured' && lag.reason ? <span className="muted"> ({lag.reason})</span> : null}
      {lag.configured_interval_seconds !== null ? (
        <span className="muted small lag-context">
          {' '}
          · configured interval {durationText(lag.configured_interval_seconds)} (context only, not a latency guarantee)
        </span>
      ) : null}
      {lag.note ? <span className="muted small lag-note"> · {lag.note}</span> : null}
    </span>
  )
}

/** "Load more" for keyset pages; the cursor is reused only with the same filters. */
export function LoadMore({
  cursor,
  loading,
  onLoad,
  what,
}: {
  cursor: string | null
  loading: boolean
  onLoad: () => void
  what: string
}) {
  if (!cursor) return <p className="muted small">End of the list.</p>
  return (
    <div className="form-actions">
      <button type="button" className="button secondary" onClick={onLoad} disabled={loading}>
        {loading ? 'Loading…' : `Load more ${what}`}
      </button>
    </div>
  )
}

export { label }
