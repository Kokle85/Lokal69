import { Link } from 'react-router'
import { useApiQuery } from '../hooks/useApiQuery'
import { label } from '../format'
import { Badge, EmptyState, ErrorPanel, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../components/ui'
import { useWorkspace } from '../workspace/WorkspaceProvider'

export function OverviewScreen() {
  const { timezone, can } = useWorkspace()
  const overview = useApiQuery((client, signal) => client.overview({ signal }), [])
  const canReadOutbox = can('reviews:read')
  const outbox = useApiQuery(
    (client, signal) => (canReadOutbox ? client.outbox({ limit: 25 }, { signal }) : Promise.reject(new Error('skip'))),
    [canReadOutbox],
  )

  return (
    <div className="screen">
      <h1>Overview</h1>
      {overview.status === 'loading' ? <LoadingState label="Loading overview" /> : null}
      {overview.error ? <ErrorPanel error={overview.error} onRetry={overview.reload} /> : null}
      {overview.data && overview.envelope ? (
        <>
          <ViewMeta
            asOf={overview.envelope.as_of}
            fetchedAt={overview.fetchedAt}
            timeZone={timezone}
            onReload={overview.reload}
            reloading={overview.reloading}
          />
          <Warnings warnings={overview.envelope.warnings} />
          {overview.data.paused_sources > 0 ? (
            <Notice tone="warn">
              {overview.data.paused_sources} source{overview.data.paused_sources === 1 ? ' is' : 's are'} paused: no new
              network work runs for {overview.data.paused_sources === 1 ? 'it' : 'them'}. Results may be incomplete.
            </Notice>
          ) : null}
          <div className="cards">
            <div className="card stat">
              <span className="stat-label">Running sources</span>
              <span className="stat-value">{overview.data.running_sources}</span>
            </div>
            <div className="card stat">
              <span className="stat-label">Paused sources</span>
              <span className="stat-value">{overview.data.paused_sources}</span>
            </div>
            <div className="card stat">
              <span className="stat-label">Last successful scan</span>
              <span className="stat-value small">
                <Timestamp value={overview.data.last_successful_scan_at} timeZone={timezone} withAge />
              </span>
            </div>
            <div className="card stat">
              <span className="stat-label">Pending reviews</span>
              <span className="stat-value">
                <Link to="/reviews">{overview.data.pending_reviews.pending}</Link>
              </span>
            </div>
            <div className="card stat">
              <span className="stat-label">Failed or uncertain deliveries</span>
              <span className="stat-value">
                {overview.data.failed_deliveries.uncertain +
                  overview.data.failed_deliveries.blocked +
                  overview.data.failed_deliveries.dead_letter +
                  overview.data.failed_deliveries.retry_wait}
              </span>
            </div>
            <div className="card stat">
              <span className="stat-label">Activation blockers</span>
              <span className="stat-value">{overview.data.activation_blockers.length}</span>
            </div>
          </div>

          <Section title="Sources" id="overview-sources">
            {overview.data.sources.length === 0 ? (
              <EmptyState>No sources are registered.</EmptyState>
            ) : (
              <table className="responsive-table">
                <thead>
                  <tr>
                    <th scope="col">Source</th>
                    <th scope="col">Country</th>
                    <th scope="col">State</th>
                    <th scope="col">Last successful scan</th>
                    <th scope="col">Pause reason</th>
                  </tr>
                </thead>
                <tbody>
                  {overview.data.sources.map((source) => (
                    <tr key={source.source_id}>
                      <td data-label="Source">
                        {source.display_name} <span className="muted">({source.source_key})</span>
                      </td>
                      <td data-label="Country">{source.country}</td>
                      <td data-label="State">
                        <Badge value={source.state} />
                      </td>
                      <td data-label="Last successful scan">
                        <Timestamp value={source.last_successful_scan_at} timeZone={timezone} withAge />
                      </td>
                      <td data-label="Pause reason">{source.pause_reason ?? <span className="muted">none</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Section>

          <Section title="Coverage gaps" id="overview-gaps">
            <p className="muted">{overview.data.coverage_note}</p>
            {overview.data.coverage_gaps.length === 0 ? (
              <EmptyState>No coverage gaps reported.</EmptyState>
            ) : (
              <ul className="item-list">
                {overview.data.coverage_gaps.map((gap, index) => (
                  <li key={`${gap.source_key}-${gap.partition_key ?? ''}-${index}`}>
                    <Badge value={gap.kind} tone="warn" /> <strong>{gap.source_key}</strong>
                    {gap.partition_key ? <span className="muted"> partition {gap.partition_key}</span> : null}
                    {gap.profile ? <span className="muted"> profile {label(gap.profile)}</span> : null}
                    {gap.since ? (
                      <span className="muted">
                        {' '}
                        since <Timestamp value={gap.since} timeZone={timezone} />
                      </span>
                    ) : null}
                    {gap.reasons.length ? (
                      <ul>
                        {gap.reasons.map((reason, i) => (
                          <li key={i}>{reason}</li>
                        ))}
                      </ul>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </Section>

          <Section title="Pending reviews by queue" id="overview-queues">
            <p>
              Pending {overview.data.pending_reviews.pending} · claimed {overview.data.pending_reviews.claimed} · needs
              information {overview.data.pending_reviews.needs_information} · watch {overview.data.pending_reviews.watch} ·
              shortlisted {overview.data.pending_reviews.shortlisted}
            </p>
            {overview.data.pending_reviews.by_queue.length ? (
              <table className="responsive-table">
                <thead>
                  <tr>
                    <th scope="col">Queue</th>
                    <th scope="col">Pending</th>
                    <th scope="col">Claimed</th>
                    <th scope="col">Needs information</th>
                  </tr>
                </thead>
                <tbody>
                  {overview.data.pending_reviews.by_queue.map((queue) => (
                    <tr key={queue.profile}>
                      <td data-label="Queue">{queue.queue_label}</td>
                      <td data-label="Pending">{queue.pending}</td>
                      <td data-label="Claimed">{queue.claimed}</td>
                      <td data-label="Needs information">{queue.needs_information}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : null}
          </Section>

          <Section title="Activation blockers" id="overview-blockers">
            <p className="muted">
              Notification bridge: <Badge value={overview.data.bridge_status} />
            </p>
            {overview.data.activation_blockers.length === 0 ? (
              <EmptyState>No activation gate is blocked.</EmptyState>
            ) : (
              <ul className="item-list">
                {overview.data.activation_blockers.map((gate) => (
                  <li key={gate.capability}>
                    <Badge value={gate.status} /> <strong>{gate.capability}</strong>: {gate.dependency}
                    {gate.next_action ? <div className="muted">Next action: {gate.next_action}</div> : null}
                  </li>
                ))}
              </ul>
            )}
          </Section>
        </>
      ) : null}

      {canReadOutbox ? (
        <Section title="Deliveries needing attention" id="overview-outbox">
          {outbox.status === 'loading' ? <LoadingState label="Loading deliveries" /> : null}
          {outbox.error ? <ErrorPanel error={outbox.error} onRetry={outbox.reload} /> : null}
          {outbox.data ? (
            outbox.data.items.length === 0 ? (
              <EmptyState>No failed, blocked or uncertain deliveries.</EmptyState>
            ) : (
              <table className="responsive-table">
                <thead>
                  <tr>
                    <th scope="col">Event</th>
                    <th scope="col">State</th>
                    <th scope="col">Attempts</th>
                    <th scope="col">Problem</th>
                    <th scope="col">Created</th>
                  </tr>
                </thead>
                <tbody>
                  {outbox.data.items.map((item) => (
                    <tr key={item.outbox_id}>
                      <td data-label="Event">
                        {item.event_type}
                        {item.is_fixture ? <Badge tone="muted">fixture</Badge> : null}
                      </td>
                      <td data-label="State">
                        <Badge value={item.state} />
                      </td>
                      <td data-label="Attempts">
                        {item.attempts} / {item.max_attempts}
                      </td>
                      <td data-label="Problem">
                        {item.last_error_code ?? item.blocker_code ?? <span className="muted">none recorded</span>}
                        {item.uncertain_notice ? <div className="muted">{item.uncertain_notice}</div> : null}
                      </td>
                      <td data-label="Created">
                        <Timestamp value={item.event_created_at} timeZone={timezone} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )
          ) : null}
        </Section>
      ) : null}
    </div>
  )
}
