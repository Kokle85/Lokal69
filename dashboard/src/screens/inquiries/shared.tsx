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
  AuthorizationStatus,
  ContactStatus,
  InquiryControlView,
  InquiryState,
  InquiryVehicleRef,
  LagView,
  ProcessBlocker,
  ReplySummaryView,
  RequestKind,
  Scope,
  SenderReadiness,
  SignalStatus,
  SuppressionReason,
  WaitingReason,
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
 * Why an inquiry waits (`waiting_reason`, typed by the server): a short label and the full
 * explanation. None of these is an approval wait: there is no per-message approval (spec 37.1).
 */
const WAITING_REASON_TEXT: Record<WaitingReason, { label: string; help: string }> = {
  UNCERTAIN_DELIVERY: {
    label: 'uncertain delivery',
    help: 'A send attempt may have reached the provider: held for reconciliation with positive evidence, never resent blindly.',
  },
  NEEDS_FACTS: {
    label: 'needs facts',
    help: 'Recipient, language or other facts are not established yet (technical checks, not an approval).',
  },
  INQUIRIES_PAUSED: {
    label: 'inquiries paused',
    help: 'The inquiry kill switch is on or the mode is paused: nothing is reserved or sent until the owner resumes.',
  },
  SENDER_SETUP_INCOMPLETE: {
    label: 'sender setup incomplete',
    help: 'The configured sending identity is not ready (technical setup of the account, not a message approval).',
  },
  WORKER_OFFLINE: {
    label: 'mail worker offline',
    help: "The desktop mail worker (classic Outlook on the owner's PC) is offline or its credential is not live: nothing is handed to Outlook until it is back.",
  },
  RATE_CAP_REACHED: {
    label: 'rolling caps reached',
    help: 'The hard caps (2 inquiries per rolling 24 hours, 5 per rolling 15 days, or lower) are used up: it waits for the window to free.',
  },
  SELLER_COOLDOWN: {
    label: 'seller cooldown',
    help: 'This seller was contacted recently: another inquiry to the same seller waits at least 7 days.',
  },
  SEND_HELD: {
    label: 'send held',
    help: 'The send is held until its window or a pre-send check allows it.',
  },
}

export function waitingReasonLabel(reason: WaitingReason): string {
  return WAITING_REASON_TEXT[reason]?.label ?? reason
}

export function waitingReasonHelp(reason: WaitingReason): string {
  return WAITING_REASON_TEXT[reason]?.help ?? 'The server reports a waiting reason this dashboard does not know yet.'
}

/** The typed waiting reason of an inquiry (or "not waiting"). */
export function WaitingReasonBadge({ reason }: { reason: WaitingReason | null }) {
  if (reason === null) return <span className="muted small">not waiting</span>
  return (
    <span data-testid="waiting-reason" data-reason={reason} title={waitingReasonHelp(reason)}>
      <Badge tone={reason === 'UNCERTAIN_DELIVERY' ? 'warn' : 'info'}>{waitingReasonLabel(reason)}</Badge>
    </span>
  )
}

const AUTHORIZATION_TEXT: Record<AuthorizationStatus, string> = {
  active: 'active',
  missing: 'not recorded',
  not_effective: 'recorded, not effective now',
  revoked: 'revoked',
}

export function authorizationText(status: AuthorizationStatus): string {
  return AUTHORIZATION_TEXT[status] ?? status
}

const SENDER_READINESS_TEXT: Record<SenderReadiness, { label: string; help: string }> = {
  ready: { label: 'ready', help: 'verified, alias verified, healthy and unrevoked' },
  missing: { label: 'missing', help: 'no sender binding is exactly the configured sending identity' },
  unverified: { label: 'not verified', help: 'the account verification has not succeeded' },
  alias_unverified: { label: 'alias not verified', help: 'the From / Reply-To alias is not verified' },
  unhealthy: { label: 'unhealthy', help: 'the account or its credential is not healthy' },
  revoked: { label: 'revoked', help: 'the owner revoked this sending identity' },
}

/** A short readiness label (for badges). */
export function senderReadinessLabel(readiness: SenderReadiness): string {
  return SENDER_READINESS_TEXT[readiness]?.label ?? readiness
}

/** The readiness label with its meaning. */
export function senderReadinessText(readiness: SenderReadiness): string {
  const text = SENDER_READINESS_TEXT[readiness]
  return text ? `${text.label} (${text.help})` : readiness
}

/** Readable meanings of the server's sender problem codes (codes only, never an address). */
const SENDER_PROBLEM_TEXT: Record<string, string> = {
  sender_binding_missing: 'no sender binding of the configured provider',
  sender_binding_revoked: 'the sender binding is revoked',
  sender_binding_unverified: 'the sender binding is not verified',
  sender_alias_unverified: 'the From / Reply-To alias is not verified',
  sender_binding_unhealthy: 'the sender account is not healthy',
  sender_identity_provider_not_configured: 'SELLER_EMAIL_PROVIDER is not configured',
  sender_identity_account_not_configured: 'SELLER_EMAIL_ACCOUNT_ID is not configured',
  sender_identity_from_not_configured: 'SELLER_EMAIL_FROM is not configured',
  sender_identity_reply_to_invalid: 'SELLER_EMAIL_REPLY_TO is invalid',
  sender_identity_sender_binding_mismatch: 'the binding is not exactly the configured identity',
}

export function SenderProblemList({ codes }: { codes: string[] }) {
  return (
    <ul className="small plain-list">
      {codes.map((code) => (
        <li key={code} data-testid="sender-problem">
          <code>{code}</code>
          {SENDER_PROBLEM_TEXT[code] ? <span className="muted"> ({SENDER_PROBLEM_TEXT[code]})</span> : null}
        </li>
      ))}
    </ul>
  )
}

/** Why the backend's PROCESS-level gate is closed, in plain words (server codes; never guessed). */
export function processBlockerText(control: InquiryControlView, code: ProcessBlocker): string {
  switch (code) {
    case 'SELLER_INQUIRY_MODE_NOT_AUTOMATIC':
      return `the backend process setting SELLER_INQUIRY_MODE is ${control.process_mode ? label(control.process_mode) : 'not reported'}, not automatic`
    case 'SELLER_INQUIRY_KILL_SWITCH_ON':
      return 'the backend process kill switch (SELLER_INQUIRY_KILL_SWITCH) is on'
    case 'MESSAGE_APPROVAL_SETTING_ON':
      return "the owner's setting SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL disables automatic sending"
    default:
      return `the backend process gate is closed (${String(code)})`
  }
}

/**
 * Why nothing can be sent now although the database controls allow it (empty: every gate is open).
 * The PROCESS-level gate of the backend comes first: the database saying "automatic" is never
 * enough, and automatic inquiries are claimed only when the server says they are possible.
 */
export function sendingReadinessProblems(control: InquiryControlView): string[] {
  const problems: string[] = []
  if (control.process_mode === null || control.process_mode === undefined) {
    problems.push('the backend did not report its process gate (SELLER_INQUIRY_MODE)')
  }
  for (const code of control.process_blockers ?? []) problems.push(processBlockerText(control, code))
  if (control.authorization_status !== 'active') {
    problems.push(`the standing authorization is ${authorizationText(control.authorization_status)}`)
  }
  if (control.sender_readiness !== 'ready') {
    problems.push(`the configured sender is ${senderReadinessText(control.sender_readiness)}`)
  } else if (control.activation_canary_complete !== true) {
    // F3/OPS-04: the server reserves no real inquiry before the owner's activation canary of the
    // configured sender's CURRENT version has a correlated test reply.
    problems.push(
      'the activation canary of the configured sender is not complete (activation_canary_incomplete): no real seller inquiry is reserved until a correlated test reply is recorded',
    )
  }
  // The owner's rolling caps (the server counts them in `automatic_inquiries_possible` too): a cap
  // of 0 holds every real inquiry (e.g. during the activation canary step); a used-up window waits.
  if (control.max_per_24h === 0 || control.max_per_15d === 0) {
    problems.push("the owner's rolling caps are 0 (every seller inquiry is held)")
  } else {
    if (control.used_24h >= control.max_per_24h) {
      problems.push(`the rolling 24-hour cap is used up (${control.used_24h} of ${control.max_per_24h})`)
    }
    if (control.used_15d >= control.max_per_15d) {
      problems.push(`the rolling 15-day cap is used up (${control.used_15d} of ${control.max_per_15d})`)
    }
  }
  if (!problems.length && control.automatic_inquiries_possible !== true) {
    problems.push('the server does not report automatic inquiries as possible')
  }
  return problems
}

/** What happened to a reply's `seller.reply.received` signal (dot activation route). */
const SIGNAL_TEXT: Record<SignalStatus, { label: string; tone: string; help: string }> = {
  emitted: { label: 'signal emitted', tone: 'ok', help: 'A private signal was queued for the dot activation route.' },
  coalesced: {
    label: 'coalesced',
    tone: 'neutral',
    help: 'A signal for this inquiry was still pending, so this reply rides on it (no second activation needed).',
  },
  rate_limited: {
    label: 'signal cap reached',
    tone: 'warn',
    help: 'The per-inquiry signal cap was reached: the reply is stored and shown here, but it started no new dot activation.',
  },
  not_applicable: { label: 'no signal', tone: 'muted', help: 'Not a matched seller reply, so no signal is sent.' },
}

export function SignalStatusBadge({ status }: { status: SignalStatus | null }) {
  if (status === null) return <span className="muted small">not recorded</span>
  const text = SIGNAL_TEXT[status] ?? { label: status, tone: 'neutral', help: status }
  return (
    <span data-testid="signal-status" data-signal={status} title={text.help}>
      <Badge tone={text.tone}>{text.label}</Badge>
    </span>
  )
}

export function signalStatusHelp(status: SignalStatus): string {
  return SIGNAL_TEXT[status]?.help ?? status
}

/**
 * The availability a reply row states. A quarantined reply is an unverified possible match that may
 * be unrelated personal mail, so its text is the owner's only (`config:admin`, the server's
 * `views.inquiries.reply_content_visible` rule): a claim DERIVED from that text is withheld from
 * everyone else, and the owner sees it labelled as an unverified match, never as a stated fact.
 */
export function ReplyAvailability({ reply }: { reply: ReplySummaryView }) {
  const { can } = useWorkspace()
  if (reply.content_withheld || (reply.quarantined && !can('config:admin'))) {
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
