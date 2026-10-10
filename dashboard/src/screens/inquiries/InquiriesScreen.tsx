import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import {
  INQUIRY_STATES,
  WAITING_REASONS,
  type InquiryControlView,
  type InquiryListQuery,
  type InquiryState,
  type InquirySummaryView,
  type WaitingReason,
} from '../../api/types'
import { Badge, EmptyState, ErrorPanel, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../../components/ui'
import { label } from '../../format'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useMorePages } from '../../hooks/useMorePages'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import {
  InquiryAreaNav,
  InquiryStateBadge,
  LoadMore,
  recipientText,
  RequireScope,
  StandingAuthorizationNote,
  suppressionText,
  vehicleText,
  WaitingReasonBadge,
  waitingReasonHelp,
  waitingReasonLabel,
} from './shared'

function filtersFrom(params: URLSearchParams): InquiryListQuery {
  const query: InquiryListQuery = { limit: 25 }
  const state = params.get('state')
  if (state && (INQUIRY_STATES as readonly string[]).includes(state)) query.state = state as InquiryState
  if (params.get('uncertain_only') === 'true') query.uncertain_only = true
  if (params.get('attention_only') === 'true') query.attention_only = true
  return query
}

export function InquiriesScreen() {
  return (
    <RequireScope scope="inquiries:read" what="seller inquiries">
      <InquiriesContent />
    </RequireScope>
  )
}

function InquiriesContent() {
  const { timezone, client } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const filters = filtersFrom(params)
  const list = useApiQuery((api, signal) => api.inquiries(filters, { signal }), [filters])
  const more = useMorePages<InquirySummaryView>(list.envelope?.request_id ?? null, list.envelope?.next_cursor ?? null, (cursor) =>
    client.inquiries({ ...filters, cursor }),
  )
  const items = [...(list.data?.items ?? []), ...more.items]

  return (
    <div className="screen">
      <h1>Seller inquiries</h1>
      <InquiryAreaNav />
      <StandingAuthorizationNote />
      <AttentionSection timeZone={timezone} />
      <Section title="All inquiries" id="inquiry-list">
        <InquiryFilters
          // Remounted per URL filter set, so Back/Forward never leaves the form showing other filters.
          key={JSON.stringify(filters)}
          filters={filters}
          onApply={(next) => {
            const search = new URLSearchParams()
            if (next.state) search.set('state', next.state)
            if (next.uncertain_only) search.set('uncertain_only', 'true')
            if (next.attention_only) search.set('attention_only', 'true')
            setParams(search)
          }}
        />
        {list.status === 'loading' ? <LoadingState label="Loading inquiries" /> : null}
        {list.error ? <ErrorPanel error={list.error} onRetry={list.reload} /> : null}
        {list.envelope ? (
          <>
            <ViewMeta asOf={list.envelope.as_of} fetchedAt={list.fetchedAt} timeZone={timezone} onReload={list.reload} reloading={list.reloading} />
            <Warnings warnings={list.envelope.warnings} />
            {items.length === 0 ? (
              <EmptyState>No seller inquiries match these filters.</EmptyState>
            ) : (
              <InquiryTable items={items} timeZone={timezone} caption="Seller inquiries" />
            )}
            {more.error ? <ErrorPanel error={more.error} onRetry={() => void more.loadMore()} /> : null}
            <LoadMore cursor={more.cursor} loading={more.loading} onLoad={() => void more.loadMore()} what="inquiries" />
          </>
        ) : null}
      </Section>
    </div>
  )
}

function InquiryFilters({ filters, onApply }: { filters: InquiryListQuery; onApply: (next: InquiryListQuery) => void }) {
  const [state, setState] = useState<string>(filters.state ?? '')
  const [uncertain, setUncertain] = useState(Boolean(filters.uncertain_only))
  const [attention, setAttention] = useState(Boolean(filters.attention_only))
  function submit(event: FormEvent) {
    event.preventDefault()
    const next: InquiryListQuery = {}
    if (state) next.state = state as InquiryState
    if (uncertain) next.uncertain_only = true
    if (attention) next.attention_only = true
    onApply(next)
  }
  return (
    <form className="filters" onSubmit={submit} aria-label="Inquiry filters">
      <div className="field">
        <label htmlFor="inquiry-state">State</label>
        <select id="inquiry-state" value={state} onChange={(event) => setState(event.target.value)}>
          <option value="">any state</option>
          {INQUIRY_STATES.map((item) => (
            <option key={item} value={item}>
              {label(item)}
            </option>
          ))}
        </select>
      </div>
      <label className="inline-check">
        <input type="checkbox" checked={uncertain} onChange={(event) => setUncertain(event.target.checked)} /> uncertain sends only
      </label>
      <label className="inline-check">
        <input type="checkbox" checked={attention} onChange={(event) => setAttention(event.target.checked)} /> needs attention only
      </label>
      <div className="field-actions">
        <button type="submit" className="button">
          Apply filters
        </button>
      </div>
    </form>
  )
}

export function InquiryTable({ items, timeZone, caption }: { items: InquirySummaryView[]; timeZone: string; caption: string }) {
  return (
    <table className="responsive-table" aria-label={caption}>
      <thead>
        <tr>
          <th scope="col">Vehicle</th>
          <th scope="col">State</th>
          <th scope="col">Waiting for</th>
          <th scope="col">Language</th>
          <th scope="col">Recipient</th>
          <th scope="col">Replies</th>
          <th scope="col">Send attempted</th>
          <th scope="col">Accepted by provider</th>
          <th scope="col">State changed</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.inquiry_id} data-testid="inquiry-row">
            <td data-label="Vehicle">
              <Link className="row-link" to={`/inquiries/${item.inquiry_id}`}>
                {vehicleText(item.vehicle)}
              </Link>
            </td>
            <td data-label="State">
              <InquiryStateBadge state={item.state} />
              {item.delivery_uncertain ? <Badge tone="warn">delivery uncertain</Badge> : null}
              {item.suppression_reason ? <span className="muted small"> {suppressionText(item.suppression_reason)}</span> : null}
            </td>
            <td data-label="Waiting for">
              <WaitingReasonBadge reason={item.waiting_reason} />
            </td>
            <td data-label="Language">{item.language ?? <span className="muted">not resolved</span>}</td>
            <td data-label="Recipient">
              <Badge value={item.recipient_status}>{recipientText(item.recipient_status)}</Badge>
            </td>
            <td data-label="Replies">{item.reply_count}</td>
            <td data-label="Send attempted">
              <Timestamp value={item.send_attempted_at} timeZone={timeZone} />
            </td>
            <td data-label="Accepted by provider">
              <Timestamp value={item.accepted_at} timeZone={timeZone} />
            </td>
            <td data-label="State changed">
              <Timestamp value={item.state_changed_at} timeZone={timeZone} />
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

interface AttentionGroup {
  key: string
  title: string
  help: string
  items: InquirySummaryView[]
}

/** Waiting reasons shown in their own groups (uncertain delivery and missing facts have theirs). */
const WAIT_GROUP_REASONS: readonly WaitingReason[] = WAITING_REASONS.filter(
  (reason) => reason !== 'UNCERTAIN_DELIVERY' && reason !== 'NEEDS_FACTS',
)

function isUncertain(item: InquirySummaryView): boolean {
  return item.state === 'uncertain' || item.delivery_uncertain || item.waiting_reason === 'UNCERTAIN_DELIVERY'
}

/**
 * The attention list grouped by what the owner should know: an uncertain outcome first, then held
 * for facts, then each typed waiting reason (caps, cooldown, pause, worker offline, sender setup,
 * held send), suppressed, failed or stuck, and anything else the server flagged. Each inquiry is
 * shown once.
 */
function attentionGroups(items: InquirySummaryView[]): AttentionGroup[] {
  const groups: AttentionGroup[] = [
    {
      key: 'uncertain',
      title: 'Uncertain send outcome',
      help: 'May have reached the provider: held for reconciliation with positive evidence, never resent blindly.',
      items: items.filter(isUncertain),
    },
    {
      key: 'held',
      title: 'Held for facts or technical checks',
      help: 'Recipient, language or other facts are not established yet; this is not an approval wait.',
      items: items.filter((item) => !isUncertain(item) && (item.state === 'held_facts' || item.waiting_reason === 'NEEDS_FACTS')),
    },
  ]
  const placed = () => new Set(groups.flatMap((group) => group.items.map((item) => item.inquiry_id)))
  for (const reason of WAIT_GROUP_REASONS) {
    const taken = placed()
    groups.push({
      key: `wait-${reason}`,
      title: `Waiting: ${waitingReasonLabel(reason)}`,
      help: waitingReasonHelp(reason),
      items: items.filter((item) => !taken.has(item.inquiry_id) && item.waiting_reason === reason),
    })
  }
  const taken = placed()
  groups.push(
    {
      key: 'suppressed',
      title: 'Suppressed',
      help: 'Will not be sent (opt-out, bounce, kill switch, contradictory availability, ...).',
      items: items.filter((item) => !taken.has(item.inquiry_id) && item.state === 'suppressed'),
    },
    {
      key: 'failed',
      title: 'Failed or stuck',
      help: 'Definite failures and attempts that are still marked as sending.',
      items: items.filter((item) => !taken.has(item.inquiry_id) && (item.state === 'failed_definite' || item.state === 'sending')),
    },
  )
  const known = placed()
  // Any waiting reason this dashboard does not know yet still shows in its own typed form.
  const other = items.filter((item) => !known.has(item.inquiry_id))
  if (other.length) groups.push({ key: 'other', title: 'Other', help: 'Other inquiries the server flagged for attention.', items: other })
  return groups
}

function capsReached(control: InquiryControlView): boolean {
  return control.used_24h >= control.max_per_24h || control.used_15d >= control.max_per_15d
}

function AttentionSection({ timeZone }: { timeZone: string }) {
  const attention = useApiQuery((api, signal) => api.inquiries({ attention_only: true, limit: 100 }, { signal }), [])
  const waiting = useApiQuery((api, signal) => api.inquiries({ state: 'qualifying', limit: 100 }, { signal }), [])
  const control = useApiQuery((api, signal) => api.inquiryControl({ signal }), [])
  // Qualified inquiries the attention list does not show already (no reported waiting reason).
  const flagged = new Set((attention.data?.items ?? []).map((item) => item.inquiry_id))
  const waitingUnlisted = (waiting.data?.items ?? []).filter((item) => !flagged.has(item.inquiry_id) && item.waiting_reason === null)
  return (
    <Section title="Needs attention" id="attention">
      <ControlSummary control={control.data} error={control.error} loading={control.status === 'loading'} />
      {attention.status === 'loading' ? <LoadingState label="Loading inquiries that need attention" /> : null}
      {attention.error ? <ErrorPanel error={attention.error} onRetry={attention.reload} /> : null}
      {attention.data ? (
        attention.data.items.length === 0 ? (
          <EmptyState>No uncertain, held, waiting, suppressed, failed or stuck inquiries.</EmptyState>
        ) : (
          attentionGroups(attention.data.items)
            .filter((group) => group.items.length > 0)
            .map((group) => (
              <div key={group.key} className="attention-group" data-testid={`attention-${group.key}`}>
                <h3>
                  {group.title} ({group.items.length})
                </h3>
                <p className="muted small">{group.help}</p>
                {group.key === 'wait-RATE_CAP_REACHED' && control.data && capsReached(control.data) ? (
                  <Notice tone="warn">
                    <span data-testid="caps-reached">
                      The rolling caps are reached ({control.data.used_24h} of {control.data.max_per_24h} in 24 h,{' '}
                      {control.data.used_15d} of {control.data.max_per_15d} in 15 days): these inquiries wait for the window to
                      free. Nothing is sent until then, and the caps are ceilings, not targets.
                    </span>
                  </Notice>
                ) : null}
                <InquiryTable items={group.items} timeZone={timeZone} caption={group.title} />
              </div>
            ))
        )
      ) : null}
      {attention.envelope?.next_cursor ? (
        <p className="muted small">
          More inquiries need attention: <Link to="/inquiries?attention_only=true">show them all</Link>.
        </p>
      ) : null}
      {waiting.error ? <ErrorPanel error={waiting.error} onRetry={waiting.reload} /> : null}
      {waitingUnlisted.length > 0 || waiting.envelope?.next_cursor ? (
        <div className="attention-group" data-testid="attention-waiting">
          <h3>Qualified, not reserved yet ({waitingUnlisted.length})</h3>
          <p className="muted small">
            Qualified inquiries without a reported waiting reason: they are reserved automatically once planning runs and the
            rolling caps, the seller cooldown and the pause allow it.
          </p>
          {waitingUnlisted.length > 0 ? (
            <InquiryTable items={waitingUnlisted} timeZone={timeZone} caption="Qualified, not reserved yet" />
          ) : (
            <p className="muted small">The qualified inquiries of this page are listed above with their waiting reason.</p>
          )}
          {waiting.envelope?.next_cursor ? (
            <p className="muted small">
              More qualified inquiries are waiting: <Link to="/inquiries?state=qualifying">show them all</Link>.
            </p>
          ) : null}
        </div>
      ) : null}
    </Section>
  )
}

function ControlSummary({
  control,
  error,
  loading,
}: {
  control: InquiryControlView | null
  error: { code: string } | null
  loading: boolean
}) {
  if (loading) return null
  if (!control) {
    return (
      <p className="muted small" data-testid="control-summary">
        {error?.code === 'NOT_FOUND'
          ? 'Inquiry controls are not set up for this workspace yet, so nothing can be sent.'
          : 'The inquiry controls could not be loaded.'}{' '}
        <Link to="/inquiry-control">Inquiry control</Link>
      </p>
    )
  }
  const stopped = control.kill_switch || control.mode !== 'automatic'
  return (
    <p className="control-summary" data-testid="control-summary">
      <Badge value={control.mode} /> {control.kill_switch ? <Badge tone="bad">kill switch on</Badge> : <Badge tone="ok">kill switch off</Badge>}{' '}
      <span className="muted">
        24 h: {control.used_24h} of {control.max_per_24h} used · 15 days: {control.used_15d} of {control.max_per_15d} used
        {stopped ? ' · sending is stopped' : ''}
      </span>{' '}
      <Link to="/inquiry-control">Inquiry control</Link>
    </p>
  )
}
