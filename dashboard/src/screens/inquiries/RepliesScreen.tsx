import { useState, type FormEvent } from 'react'
import { Link, useSearchParams } from 'react-router'
import { LIMITS, type ReplyListQuery, type ReplySummaryView } from '../../api/types'
import { Badge, EmptyState, ErrorPanel, LoadingState, Timestamp, ViewMeta, Warnings } from '../../components/ui'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useMorePages } from '../../hooks/useMorePages'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import { InquiryAreaNav, LoadMore, ReplyAvailability, RequireScope, SignalStatusBadge, StandingAuthorizationNote, vehicleText } from './shared'

function filtersFrom(params: URLSearchParams): ReplyListQuery {
  const query: ReplyListQuery = { limit: 25 }
  const inquiry = params.get('inquiry_id')
  if (inquiry && LIMITS.uuidPattern.test(inquiry)) query.inquiry_id = inquiry
  if (params.get('quarantined_only') === 'true') query.quarantined_only = true
  return query
}

export function RepliesScreen() {
  return (
    <RequireScope scope="inquiries:read" what="seller replies">
      <RepliesContent />
    </RequireScope>
  )
}

function RepliesContent() {
  const { timezone, client } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const filters = filtersFrom(params)
  const list = useApiQuery((api, signal) => api.replies(filters, { signal }), [filters])
  const more = useMorePages<ReplySummaryView>(list.envelope?.request_id ?? null, list.envelope?.next_cursor ?? null, (cursor) =>
    client.replies({ ...filters, cursor }),
  )
  const items = [...(list.data?.items ?? []), ...more.items]

  return (
    <div className="screen">
      <h1>Seller replies</h1>
      <InquiryAreaNav />
      <StandingAuthorizationNote />
      <p className="muted small">
        Only replies correlated with this system&apos;s inquiries are stored. A quarantined reply is an unverified possible match
        (forwarded, changed address, ambiguous); its text is shown to the owner only. No reply is ever sent automatically. The
        dot signal column says whether a reply started a dot activation: a reply past the per-inquiry signal cap is stored and
        shown here, but starts no new activation.
      </p>
      <ReplyFilters
        // Remounted per URL filter set, so Back/Forward never leaves the form showing other filters.
        key={JSON.stringify(filters)}
        filters={filters}
        onApply={(next) => {
          const search = new URLSearchParams()
          if (next.inquiry_id) search.set('inquiry_id', next.inquiry_id)
          if (next.quarantined_only) search.set('quarantined_only', 'true')
          setParams(search)
        }}
      />
      {list.status === 'loading' ? <LoadingState label="Loading replies" /> : null}
      {list.error ? <ErrorPanel error={list.error} onRetry={list.reload} /> : null}
      {list.envelope ? (
        <>
          <ViewMeta asOf={list.envelope.as_of} fetchedAt={list.fetchedAt} timeZone={timezone} onReload={list.reload} reloading={list.reloading} />
          <Warnings warnings={list.envelope.warnings} />
          {items.length === 0 ? (
            <EmptyState>No replies match these filters.</EmptyState>
          ) : (
            <table className="responsive-table" aria-label="Seller replies">
              <thead>
                <tr>
                  <th scope="col">Received</th>
                  <th scope="col">Vehicle</th>
                  <th scope="col">Type</th>
                  <th scope="col">Language</th>
                  <th scope="col">Availability stated</th>
                  <th scope="col">Correlation</th>
                  <th scope="col">Processing</th>
                  <th scope="col">Dot signal</th>
                </tr>
              </thead>
              <tbody>
                {items.map((reply) => (
                  <tr key={reply.reply_id} data-testid="reply-row">
                    <td data-label="Received">
                      <Link className="row-link" to={`/replies/${reply.reply_id}`}>
                        <Timestamp value={reply.received_at} timeZone={timezone} />
                      </Link>
                    </td>
                    <td data-label="Vehicle">
                      <Link to={`/inquiries/${reply.inquiry_id}`}>{vehicleText(reply.vehicle)}</Link>
                    </td>
                    <td data-label="Type">
                      <Badge value={reply.message_type} tone={reply.message_type === 'seller_reply' ? 'ok' : 'neutral'} />
                    </td>
                    <td data-label="Language">{reply.original_language ?? 'unknown'}</td>
                    <td data-label="Availability stated">
                      <ReplyAvailability reply={reply} />
                    </td>
                    <td data-label="Correlation">
                      {reply.quarantined ? <Badge tone="warn">quarantined: unverified match</Badge> : <Badge tone="ok">matched</Badge>}
                    </td>
                    <td data-label="Processing">
                      <Badge value={reply.processing_state} />
                    </td>
                    <td data-label="Dot signal">
                      <SignalStatusBadge status={reply.signal_status} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {more.error ? <ErrorPanel error={more.error} onRetry={() => void more.loadMore()} /> : null}
          <LoadMore cursor={more.cursor} loading={more.loading} onLoad={() => void more.loadMore()} what="replies" />
        </>
      ) : null}
    </div>
  )
}

function ReplyFilters({ filters, onApply }: { filters: ReplyListQuery; onApply: (next: ReplyListQuery) => void }) {
  const [quarantined, setQuarantined] = useState(Boolean(filters.quarantined_only))
  function apply(event: FormEvent) {
    event.preventDefault()
    const next: ReplyListQuery = {}
    if (filters.inquiry_id) next.inquiry_id = filters.inquiry_id
    if (quarantined) next.quarantined_only = true
    onApply(next)
  }
  return (
    <form className="filters" onSubmit={apply} aria-label="Reply filters">
      {filters.inquiry_id ? (
        <p className="muted small">
          Showing one inquiry&apos;s replies. <Link to="/replies">Show all replies</Link>
        </p>
      ) : null}
      <label className="inline-check">
        <input type="checkbox" checked={quarantined} onChange={(event) => setQuarantined(event.target.checked)} /> quarantined only
      </label>
      <div className="field-actions">
        <button type="submit" className="button">
          Apply filters
        </button>
      </div>
    </form>
  )
}
