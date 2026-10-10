import { Link } from 'react-router'
import type { EvaluationOutcome, EvaluationReport, MoneyView } from '../api/types'
import { Badge, EmptyState, ErrorPanel, KeyValues, LoadingState, Notice, Section, Timestamp, ViewMeta, Warnings } from '../components/ui'
import { decimalText, durationText, label, ratioPercentText } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'
import { InquiryAreaNav, RequireScope } from './inquiries/shared'

const OUTCOME_TEXT: Record<EvaluationOutcome, string> = {
  coverage_not_established: 'Usable source coverage is not established yet, so the 15-day window has not started.',
  deal_found: 'A well-matched candidate meets the owner-approved threshold (a research result, not a purchase).',
  candidates_need_owner_judgement:
    'Well-matched candidate(s) show a positive supported contribution, but no owner-approved threshold exists: they need your judgement.',
  no_suitable_deal_yet: 'No suitable deal found so far in this window.',
  no_suitable_deal: 'No suitable deal was found in this window.',
}

function money(value: MoneyView): string {
  return decimalText(value.amount, value.currency)
}

export function EvaluationScreen() {
  return (
    <RequireScope scope="inquiries:read" what="the 15-day evaluation">
      <EvaluationContent />
    </RequireScope>
  )
}

function EvaluationContent() {
  const { timezone } = useWorkspace()
  const report = useApiQuery((api, signal) => api.evaluation({ days: 15 }, { signal }), [])
  return (
    <div className="screen">
      <h1>15-day evaluation</h1>
      <InquiryAreaNav />
      <p className="muted small">
        Built from stored evidence only. The goal is one genuinely useful deal in a 15-day window of working coverage; it is a
        quality objective, not a volume target. Zero is reported as zero and nothing is invented. Synthetic fixture and canary
        records never count.
      </p>
      {report.status === 'loading' ? <LoadingState label="Loading the evaluation" /> : null}
      {report.error ? <ErrorPanel error={report.error} onRetry={report.reload} /> : null}
      {report.data && report.envelope ? (
        <>
          <ViewMeta asOf={report.envelope.as_of} fetchedAt={report.fetchedAt} timeZone={timezone} onReload={report.reload} reloading={report.reloading} />
          <Warnings warnings={report.envelope.warnings} />
          <EvaluationBody report={report.data} timeZone={timezone} />
        </>
      ) : null}
    </div>
  )
}

function EvaluationBody({ report, timeZone }: { report: EvaluationReport; timeZone: string }) {
  const suitable = report.qualifying_deal_ids.length
  const inquiries = report.inquiries
  return (
    <>
      <Section title="Outcome" id="evaluation-outcome">
        <Notice tone={report.outcome === 'deal_found' ? 'ok' : report.outcome === 'candidates_need_owner_judgement' ? 'info' : 'warn'}>
          <span data-testid="evaluation-outcome">{OUTCOME_TEXT[report.outcome]}</span>
        </Notice>
        <KeyValues
          items={[
            ['Suitable deals', <strong key="s" data-testid="suitable-deals">{String(suitable)}</strong>],
            ['Candidates needing your judgement', String(report.owner_judgement_candidate_ids.length)],
            ['Window', <Badge key="w" value={report.window_status} tone={report.window_status === 'not_started' ? 'warn' : 'info'} />],
            ['Window start', report.window_start ? <Timestamp key="ws" value={report.window_start} timeZone={timeZone} /> : 'not started'],
            ['Window end', report.window_end ? <Timestamp key="we" value={report.window_end} timeZone={timeZone} /> : 'not started'],
            ['Generated', <Timestamp key="g" value={report.generated_at} timeZone={timeZone} />],
            ['Excluded synthetic records', String(report.excluded_synthetic_records)],
          ]}
        />
        {report.reasons.length ? (
          <>
            <h3>Reasons</h3>
            <ul data-testid="evaluation-reasons">
              {report.reasons.map((reason, index) => (
                <li key={index}>{reason}</li>
              ))}
            </ul>
          </>
        ) : null}
        {suitable > 0 ? (
          <p>
            Qualifying candidates:{' '}
            {report.qualifying_deal_ids.map((id) => (
              <Link key={id} to={`/candidates/${id}`} className="id-link">
                {id.slice(0, 8)}
              </Link>
            ))}
          </p>
        ) : null}
        {report.owner_judgement_candidate_ids.length ? (
          <p>
            Candidates for your judgement:{' '}
            {report.owner_judgement_candidate_ids.map((id) => (
              <Link key={id} to={`/candidates/${id}`} className="id-link">
                {id.slice(0, 8)}
              </Link>
            ))}
          </p>
        ) : null}
      </Section>

      <Section title="Healthy coverage" id="evaluation-coverage">
        <p>
          Sources with healthy coverage: <strong data-testid="healthy-sources">{report.sources_with_healthy_coverage}</strong>
        </p>
        {report.coverage.length === 0 ? (
          <EmptyState>No coverage intervals in this window.</EmptyState>
        ) : (
          <table className="responsive-table" aria-label="Coverage per source">
            <thead>
              <tr>
                <th scope="col">Source</th>
                <th scope="col">Healthy time</th>
                <th scope="col">Share of the elapsed window</th>
                <th scope="col">Healthy intervals</th>
                <th scope="col">Gaps</th>
              </tr>
            </thead>
            <tbody>
              {report.coverage.map((source) => (
                <tr key={source.source_key}>
                  <td data-label="Source">{source.source_key}</td>
                  <td data-label="Healthy time">{durationText(source.healthy_seconds)}</td>
                  <td data-label="Share of the elapsed window">{ratioPercentText(source.coverage_ratio)}</td>
                  <td data-label="Healthy intervals">
                    {source.intervals.length === 0 ? (
                      <span className="muted">none</span>
                    ) : (
                      <ul className="small" data-testid="coverage-intervals">
                        {source.intervals.map((interval, index) => (
                          <li key={index}>
                            <Timestamp value={interval.start} timeZone={timeZone} /> to{' '}
                            <Timestamp value={interval.end} timeZone={timeZone} /> ({interval.scans} healthy scans)
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                  <td data-label="Gaps">
                    {source.gaps.length === 0 ? (
                      <span className="muted">none</span>
                    ) : (
                      <ul className="small">
                        {source.gaps.map((gap, index) => (
                          <li key={index}>
                            {label(gap.reason)}: <Timestamp value={gap.start} timeZone={timeZone} /> to{' '}
                            <Timestamp value={gap.end} timeZone={timeZone} />
                          </li>
                        ))}
                      </ul>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Candidates, inquiries and replies" id="evaluation-counts">
        <div className="stats-grid">
          <Stat title="Eligible vehicles" value={report.eligible_vehicles} />
          <Stat title="Unique well-matched candidates" value={report.well_matched_vehicles} />
          <Stat title="Small-sample matches" value={report.small_sample_matches} />
          <Stat title="Inquiries sent (attempted)" value={inquiries.attempted} testId="inquiries-sent" />
          <Stat title="Accepted by the provider (not delivery)" value={inquiries.accepted} />
          <Stat title="Uncertain sends" value={inquiries.uncertain} />
          <Stat title="Seller replies" value={report.seller_replies} testId="seller-replies" />
          <Stat title="Missing documents resolved" value={report.missing_documents_resolved} testId="documents-resolved" />
        </div>
        <KeyValues
          items={[
            ['Failed definitively', String(inquiries.failed_definite)],
            ['Suppressed', String(inquiries.suppressed)],
            ['Held for facts', String(inquiries.held_for_facts)],
            ['Cancelled', String(inquiries.cancelled)],
            ['In progress', String(inquiries.in_progress)],
            ['Inquiries with a seller reply', String(inquiries.with_seller_reply)],
            [
              'Suppression reasons',
              inquiries.suppression_reasons.length
                ? inquiries.suppression_reasons.map(([reason, count]) => `${label(reason)}: ${count}`).join(', ')
                : 'none',
            ],
            ['Auto-replies (not replies)', String(report.auto_replies)],
            ['Bounces', String(report.bounces)],
            ['Delivery notices', String(report.delivery_notices)],
          ]}
        />
      </Section>

      <Section title="Best supported economics" id="evaluation-economics">
        {report.best_supported_economics === null ? (
          <p data-testid="best-economics">
            <span className="status-unknown">unknown</span>: no candidate has a complete valuation in this window.
          </p>
        ) : (
          <>
            <p className="muted small">A research estimate from a complete valuation, never a confirmed profit or an agreed price.</p>
            <KeyValues
              items={[
                [
                  'Candidate',
                  <Link key="c" to={`/candidates/${report.best_supported_economics.candidate_id}`}>
                    {report.best_supported_economics.candidate_id.slice(0, 8)}
                  </Link>,
                ],
                ['Valuation state', label(report.best_supported_economics.valuation_state)],
                [
                  `Conservative ${report.best_supported_economics.label}`,
                  <span key="cc" className="amount" data-testid="best-economics">
                    {money(report.best_supported_economics.conservative_contribution)}
                  </span>,
                ],
                [`Base ${report.best_supported_economics.label}`, <span key="bc" className="amount">{money(report.best_supported_economics.base_contribution)}</span>],
                [
                  'Owner-approved threshold',
                  report.best_supported_economics.meets_approved_threshold === null
                    ? 'no owner-approved threshold (the EUR 1,500 threshold is only PROPOSED)'
                    : report.best_supported_economics.meets_approved_threshold
                      ? 'met'
                      : 'not met',
                ],
                ['Unknowns', report.best_supported_economics.unknowns.length ? report.best_supported_economics.unknowns.join(', ') : 'none'],
              ]}
            />
          </>
        )}
        <KeyValues
          items={[
            ['Vehicles with incomplete economics', String(report.vehicles_with_incomplete_economics)],
            [
              'Most common unknowns',
              report.most_common_unknowns.length
                ? report.most_common_unknowns.map(([name, count]) => `${label(name)} (${count})`).join(', ')
                : 'none recorded',
            ],
          ]}
        />
        <p className="muted small">Volume is not a goal: there are no targets for listings, alerts or e-mails.</p>
      </Section>
    </>
  )
}

function Stat({ title, value, testId }: { title: string; value: number; testId?: string }) {
  return (
    <div className="stat" data-testid={testId}>
      <span className="stat-label">{title}</span>
      <span className="stat-value">{value}</span>
    </div>
  )
}
