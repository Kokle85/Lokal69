import type { SourceStatusView } from '../api/types'
import {
  Badge,
  EmptyState,
  ErrorPanel,
  ExternalLink,
  KeyValues,
  LoadingState,
  Section,
  Timestamp,
  ViewMeta,
  Warnings,
} from '../components/ui'
import { label, yesNo } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'
import { SourceSwitch } from './SourceSwitch'

export function SourcesScreen() {
  const { timezone } = useWorkspace()
  const sources = useApiQuery((client, signal) => client.sources({ signal }), [])
  return (
    <div className="screen">
      <h1>Sources</h1>
      {sources.status === 'loading' ? <LoadingState label="Loading sources" /> : null}
      {sources.error ? <ErrorPanel error={sources.error} onRetry={sources.reload} /> : null}
      {sources.data && sources.envelope ? (
        <>
          <ViewMeta asOf={sources.envelope.as_of} fetchedAt={sources.fetchedAt} timeZone={timezone} onReload={sources.reload} reloading={sources.reloading} />
          <Warnings warnings={sources.envelope.warnings} />
          {sources.data.items.length === 0 ? <EmptyState>No sources are registered.</EmptyState> : null}
          {sources.data.items.map((source) => (
            <SourceCard key={source.source_id} source={source} timeZone={timezone} onChanged={sources.reload} />
          ))}
        </>
      ) : null}
    </div>
  )
}

function SourceCard({ source, timeZone, onChanged }: { source: SourceStatusView; timeZone: string; onChanged: () => void }) {
  const budget = source.rate_budget
  return (
    <Section
      title={source.display_name}
      id={`source-${source.source_key}`}
      actions={<Badge value={source.state} />}
    >
      <p className="muted">
        {source.source_key} · {source.country} · {label(source.role)} · enabled in the configuration {yesNo(source.enabled)} ·
        version {source.version}
      </p>
      <SourceSwitch source={source} timeZone={timeZone} onChanged={onChanged} />
      <div className="two-columns">
        <div>
          <h3>Terms (audit record)</h3>
          <KeyValues
            items={[
              ['Terms status', <Badge key="s" value={source.terms.status} tone={source.terms.status === 'restricted' ? 'bad' : 'neutral'} />],
              ['Owner decision', <Badge key="d" value={source.terms.decision} tone={source.terms.decision === 'do_not_use' ? 'bad' : 'neutral'} />],
              ['Decided by', source.terms.decision_actor ?? 'nobody yet'],
              ['Note', source.terms.decision_note ?? 'none'],
              ['Reviewed', <Timestamp key="r" value={source.terms.reviewed_at} timeZone={timeZone} />],
              ['Terms page', source.terms.terms_url ? <ExternalLink key="u" href={source.terms.terms_url}>terms</ExternalLink> : 'not recorded'],
            ]}
          />
          <p className="muted small">{source.terms.meaning}</p>
        </div>
        <div>
          <h3>Technical status (separate from terms)</h3>
          <KeyValues
            items={[
              ['Technical status', <Badge key="t" value={source.technical.status} />],
              ['Mode / adapter', `${label(source.technical.mode)} · ${source.technical.adapter} ${source.technical.adapter_version}`],
              ['Detail mode', label(source.technical.detail_mode)],
              ['Last live smoke test', <Timestamp key="l" value={source.technical.last_live_smoke_at} timeZone={timeZone} />],
              [
                'Parser health',
                <span key="p">
                  <Badge value={source.technical.parser_health.status} /> (sample {source.technical.parser_health.sample_size})
                </span>,
              ],
              ['Robots', `${source.robots.policy}${source.robots.summary ? ` · ${source.robots.summary}` : ''}`],
            ]}
          />
          {source.technical.parser_health.reasons.length ? (
            <ul className="small">
              {source.technical.parser_health.reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          ) : null}
        </div>
      </div>
      <h3>Rate budget</h3>
      <KeyValues
        items={[
          ['Budget', `${label(budget.budget_label)}`],
          ['Requests today', budget.requests_today === null ? 'unknown' : `${budget.requests_today} of ${budget.budget.daily_request_budget ?? 'unknown'}`],
          ['Minimum delay', budget.budget.min_delay_seconds !== undefined ? `${budget.budget.min_delay_seconds} s` : 'unknown'],
          ['Search pages per run', String(budget.budget.max_search_pages_per_run ?? 'unknown')],
          ['Circuit', label(budget.circuit_state)],
          ['Next request not before', <Timestamp key="n" value={budget.next_request_not_before} timeZone={timeZone} />],
        ]}
      />
      {source.activation_problems.length ? (
        <>
          <h3>Activation problems</h3>
          <ul>
            {source.activation_problems.map((problem, index) => (
              <li key={index}>{problem}</li>
            ))}
          </ul>
        </>
      ) : null}
      <h3>Recent runs</h3>
      {source.last_runs.length === 0 ? (
        <EmptyState>No runs yet.</EmptyState>
      ) : (
        <table className="responsive-table">
          <thead>
            <tr>
              <th scope="col">Started</th>
              <th scope="col">Outcome</th>
              <th scope="col">Partition</th>
              <th scope="col">Pages / cards</th>
              <th scope="col">New / changed</th>
              <th scope="col">Gaps</th>
            </tr>
          </thead>
          <tbody>
            {source.last_runs.map((run) => (
              <tr key={run.run_id}>
                <td data-label="Started">
                  <Timestamp value={run.started_at} timeZone={timeZone} />
                </td>
                <td data-label="Outcome">
                  <Badge value={run.outcome} tone={run.outcome === 'complete' ? 'ok' : run.outcome === 'running' ? 'info' : 'warn'} />
                </td>
                <td data-label="Partition">{run.partition_key}</td>
                <td data-label="Pages / cards">
                  {run.pages_fetched} / {run.cards_seen}
                </td>
                <td data-label="New / changed">
                  {run.new_listings} / {run.changed_listings}
                </td>
                <td data-label="Gaps">{run.gap_reasons.length ? run.gap_reasons.join('; ') : <span className="muted">none</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  )
}
