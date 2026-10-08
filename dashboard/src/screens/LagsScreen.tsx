import { Link, useParams } from 'react-router'
import { Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Section, Timestamp, ViewMeta, Warnings } from '../components/ui'
import { label } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'
import { LagValue } from './inquiries/shared'

/** `GET /api/lifecycle/lags`: separate lags (spec 37.9); unknown is shown as unknown, never zero. */
export function LagsScreen() {
  const { timezone } = useWorkspace()
  const lags = useApiQuery((api, signal) => api.lifecycleLags({ signal }), [])
  return (
    <div className="screen">
      <h1>Coverage and lags</h1>
      <p className="muted small">
        Each lag is measured separately. A configured interval (a 15-minute schedule, a 2-minute mail reconciliation) is context
        only, never an observed end-to-end latency. Detection delay exists only where the source publishes a trustworthy
        timestamp; see each listing&apos;s lifecycle page.
      </p>
      {lags.status === 'loading' ? <LoadingState label="Loading lags" /> : null}
      {lags.error ? <ErrorPanel error={lags.error} onRetry={lags.reload} /> : null}
      {lags.data && lags.envelope ? (
        <>
          <ViewMeta asOf={lags.envelope.as_of} fetchedAt={lags.fetchedAt} timeZone={timezone} onReload={lags.reload} reloading={lags.reloading} />
          <Warnings warnings={lags.envelope.warnings} />
          <Section title="Pipeline lags" id="pipeline-lags">
            <KeyValues
              items={[
                ['Notification processing lag', <LagValue key="n" lag={lags.data.notification_processing_lag} />],
                ['Mail-reply detection lag', <LagValue key="m" lag={lags.data.mail_reply_detection_lag} />],
              ]}
            />
          </Section>
          <Section title="Source scan lag" id="source-lags">
            {lags.data.sources.length === 0 ? (
              <EmptyState>No sources.</EmptyState>
            ) : (
              <table className="responsive-table" aria-label="Source scan lags">
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col">State</th>
                    <th scope="col">Scan lag</th>
                    <th scope="col">Last complete scan</th>
                    <th scope="col">Last successful scan</th>
                  </tr>
                </thead>
                <tbody>
                  {lags.data.sources.map((source) => (
                    <tr key={source.source_id} data-testid="source-lag">
                      <td data-label="Source">{source.source_key}</td>
                      <td data-label="State">
                        <Badge value={source.state} />
                      </td>
                      <td data-label="Scan lag">
                        <LagValue lag={source.source_scan_lag} />
                      </td>
                      <td data-label="Last complete scan">
                        <Timestamp value={source.last_complete_scan_at} timeZone={timezone} />
                      </td>
                      <td data-label="Last successful scan">
                        <Timestamp value={source.last_successful_scan_at} timeZone={timezone} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>
          {lags.data.notes.length || lags.data.v11_tables.some((table) => !table.present) ? (
            <Section title="Notes" id="lag-notes">
              <ul className="small">
                {lags.data.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
                {lags.data.v11_tables
                  .filter((table) => !table.present)
                  .map((table) => (
                    <li key={table.relation}>
                      <code>{table.relation}</code> is not present: its lag cannot be measured (shown as unknown).
                    </li>
                  ))}
              </ul>
            </Section>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

/** `GET /api/listings/{listing_id}/lifecycle`: one source listing's own lifecycle evidence. */
export function ListingLifecycleScreen() {
  const { listingId = '' } = useParams()
  const { timezone } = useWorkspace()
  const lifecycle = useApiQuery((api, signal) => api.listingLifecycle(listingId, { signal }), [listingId])
  return (
    <div className="screen">
      <p>
        <Link to={`/candidates/${listingId}`}>← Candidate detail</Link> · <Link to="/lifecycle">All lags</Link>
      </p>
      <h1>Listing lifecycle</h1>
      {lifecycle.status === 'loading' ? <LoadingState label="Loading the listing lifecycle" /> : null}
      {lifecycle.error ? <ErrorPanel error={lifecycle.error} onRetry={lifecycle.reload} /> : null}
      {lifecycle.data && lifecycle.envelope ? (
        <>
          <ViewMeta
            asOf={lifecycle.envelope.as_of}
            fetchedAt={lifecycle.fetchedAt}
            timeZone={timezone}
            onReload={lifecycle.reload}
            reloading={lifecycle.reloading}
          />
          <Warnings warnings={lifecycle.envelope.warnings} />
          <Section title="Evidence" id="listing-lifecycle">
            <KeyValues
              items={[
                ['Source', `${lifecycle.data.source_key} (${label(lifecycle.data.source_state)})`],
                ['Availability', <Badge key="a" value={lifecycle.data.availability} />],
                ['First seen by this system', <Timestamp key="f" value={lifecycle.data.first_seen_at} timeZone={timezone} withAge />],
                ['Last seen on a search page', <Timestamp key="s" value={lifecycle.data.last_seen_on_search_at} timeZone={timezone} withAge />],
                ['Last successful detail check', <Timestamp key="d" value={lifecycle.data.last_detail_success_at} timeZone={timezone} withAge />],
                ['Last availability check', <Timestamp key="c" value={lifecycle.data.last_availability_check_at} timeZone={timezone} withAge />],
                ['Last complete source scan', <Timestamp key="l" value={lifecycle.data.last_complete_source_scan_at} timeZone={timezone} withAge />],
                [
                  'Source-reported publication',
                  lifecycle.data.source_published_at ? (
                    <span key="p">
                      <Timestamp value={lifecycle.data.source_published_at} timeZone={timezone} />{' '}
                      {lifecycle.data.source_published_trusted ? (
                        <Badge tone="ok">trusted</Badge>
                      ) : (
                        <Badge tone="warn">not trusted</Badge>
                      )}
                    </span>
                  ) : (
                    <span key="pn" className="muted">
                      not provided by the source
                    </span>
                  ),
                ],
                ['Detail freshness', <LagValue key="df" lag={lifecycle.data.detail_freshness} />],
                ['Detection delay', <LagValue key="dd" lag={lifecycle.data.detection_delay} />],
                ['Availability history from', label(lifecycle.data.availability_history_source)],
              ]}
            />
            <p className="muted small">
              &quot;First seen by this system&quot; is not the advert&apos;s age. Without a trustworthy source publication time the
              true detection delay is unknown. A missing search result, a reorder or an inaccessible page is not a sale.
            </p>
            {lifecycle.data.notes.length ? (
              <ul className="small">
                {lifecycle.data.notes.map((note, index) => (
                  <li key={index}>{note}</li>
                ))}
              </ul>
            ) : null}
          </Section>
        </>
      ) : null}
    </div>
  )
}
