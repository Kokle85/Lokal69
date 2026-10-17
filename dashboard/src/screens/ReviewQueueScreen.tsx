import { useState } from 'react'
import { Link, useSearchParams } from 'react-router'
import { isApiError } from '../api/errors'
import type { ReviewQueueItem } from '../api/types'
import { Amount, Badge, EmptyState, ErrorPanel, LoadingState, Notice, Timestamp, ViewMeta, Warnings } from '../components/ui'
import { kmText, label } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'
import type { MorePages } from './CandidatesScreen'

export function ReviewQueueScreen() {
  const { timezone, client } = useWorkspace()
  const [params, setParams] = useSearchParams()
  const includeNeedsInformation = params.get('include_needs_information') !== 'false'
  const page = useApiQuery(
    (c, signal) => c.reviews({ include_needs_information: includeNeedsInformation, limit: 25 }, { signal }),
    [includeNeedsInformation],
  )
  const [more, setMore] = useState<MorePages<ReviewQueueItem> | null>(null)
  const firstPage = page.envelope?.request_id ?? null
  const current = more && more.base === firstPage ? more : null
  const cursor = current ? current.cursor : (page.envelope?.next_cursor ?? null)
  const [loadingMore, setLoadingMore] = useState(false)

  async function loadMore() {
    if (!cursor || loadingMore || !firstPage) return
    setLoadingMore(true)
    try {
      const { envelope } = await client.reviews({ include_needs_information: includeNeedsInformation, limit: 25, cursor })
      setMore({ base: firstPage, items: [...(current?.items ?? []), ...envelope.data.items], cursor: envelope.next_cursor, error: null })
    } catch (error) {
      if (isApiError(error)) setMore({ base: firstPage, items: current?.items ?? [], cursor, error })
    } finally {
      setLoadingMore(false)
    }
  }

  const moreError = current?.error ?? null
  const items = [...(page.data?.items ?? []), ...(current?.items ?? [])]
  return (
    <div className="screen">
      <h1>Review queue</h1>
      <label className="inline-check">
        <input
          type="checkbox"
          checked={includeNeedsInformation}
          onChange={(event) => setParams(event.target.checked ? {} : { include_needs_information: 'false' })}
        />{' '}
        Include cases waiting for information
      </label>
      {page.status === 'loading' ? <LoadingState label="Loading review queue" /> : null}
      {page.error ? <ErrorPanel error={page.error} onRetry={page.reload} /> : null}
      {page.data && page.envelope ? (
        <>
          <ViewMeta asOf={page.envelope.as_of} fetchedAt={page.fetchedAt} timeZone={timezone} onReload={page.reload} reloading={page.reloading} />
          <Warnings warnings={page.envelope.warnings} />
          <Notice tone="info">
            {page.data.notice} {page.data.total} case{page.data.total === 1 ? '' : 's'} in this snapshot; it expires{' '}
            <Timestamp value={page.data.snapshot_expires_at} timeZone={timezone} />.
          </Notice>
          {items.length === 0 ? (
            <EmptyState>No cases are waiting for review.</EmptyState>
          ) : (
            <table className="responsive-table" aria-label="Review cases">
              <thead>
                <tr>
                  <th scope="col">Vehicle</th>
                  <th scope="col">Queue</th>
                  <th scope="col">State</th>
                  <th scope="col">Claim</th>
                  <th scope="col">Price</th>
                  <th scope="col">Mileage</th>
                  <th scope="col">Country</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr key={item.case_id}>
                    <td data-label="Vehicle">
                      <Link to={`/reviews/${item.case_id}`} className="row-link">
                        {[item.make, item.model].filter(Boolean).join(' ') || 'model unknown'}
                      </Link>
                      <div className="muted small">
                        <span className="untrusted">{item.title ?? 'untitled'}</span>
                      </div>
                      {item.is_fixture ? <Badge tone="muted">synthetic fixture</Badge> : null}
                    </td>
                    <td data-label="Queue">
                      {item.queue_label}
                      <div className="muted small">readiness {label(item.readiness)}</div>
                    </td>
                    <td data-label="State">
                      <Badge value={item.state} />
                      <div className="muted small">version {item.case_version}</div>
                    </td>
                    <td data-label="Claim">
                      {item.claim.claimed ? (
                        item.claim.held_by_caller ? (
                          <Badge tone="info">claimed by you</Badge>
                        ) : (
                          <Badge tone="warn">claimed by another reviewer</Badge>
                        )
                      ) : (
                        <span className="muted">unclaimed</span>
                      )}
                    </td>
                    <td data-label="Price">
                      <Amount value={item.payable} />
                      <div className="muted small">
                        EUR equivalent <Amount value={item.payable_eur} />
                      </div>
                    </td>
                    <td data-label="Mileage">{kmText(item.mileage_km)}</td>
                    <td data-label="Country">{item.seller_country ?? 'unknown'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {moreError ? <ErrorPanel error={moreError} onRetry={page.reload} retryLabel="Restart from the first page" /> : null}
          {cursor ? (
            <button type="button" className="button secondary" onClick={() => void loadMore()} disabled={loadingMore}>
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          ) : null}
        </>
      ) : null}
    </div>
  )
}
