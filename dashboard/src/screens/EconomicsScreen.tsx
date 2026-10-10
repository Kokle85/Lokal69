import { useState } from 'react'
import { Link, useParams } from 'react-router'
import type { ComparableSetView, CostLineView, EvidenceStatsView, ScenarioView, ValuationView } from '../api/types'
import {
  Amount,
  Badge,
  EmptyState,
  ErrorPanel,
  ExternalLink,
  KeyValues,
  LoadingState,
  Notice,
  Section,
  Timestamp,
  ViewMeta,
  Warnings,
} from '../components/ui'
import { decimalText, groupDecimal, kmText, label } from '../format'
import { useApiQuery } from '../hooks/useApiQuery'
import { useWorkspace } from '../workspace/WorkspaceProvider'

const CONTRIBUTION_LABEL = 'estimated contribution before business tax'

/** `/candidates/:listingId/economics` (resolves the latest valuation) or `/valuations/:valuationId`. */
export function EconomicsScreen() {
  const { listingId, valuationId } = useParams()
  const candidate = useApiQuery(
    (client, signal) => (listingId ? client.candidate(listingId, null, { signal }) : Promise.reject(new Error('no listing'))),
    [listingId ?? null],
  )
  if (valuationId) return <ValuationPanel valuationId={valuationId} comparableSetId={null} backTo={null} />
  if (candidate.status === 'loading') return <LoadingState label="Loading candidate" />
  if (candidate.error) return <ErrorPanel error={candidate.error} onRetry={candidate.reload} />
  const detail = candidate.data
  if (!detail) return null
  if (!detail.latest_valuation) {
    return (
      <div className="screen">
        <p>
          <Link to={`/candidates/${detail.summary.listing_id}`}>← Candidate</Link>
        </p>
        <h1>Economics</h1>
        <EmptyState>No valuation exists for this candidate yet.</EmptyState>
      </div>
    )
  }
  return (
    <ValuationPanel
      valuationId={detail.latest_valuation.valuation_id}
      comparableSetId={detail.comparable_set?.comparable_set_id ?? null}
      backTo={`/candidates/${detail.summary.listing_id}`}
    />
  )
}

function ValuationPanel({ valuationId, comparableSetId, backTo }: { valuationId: string; comparableSetId: string | null; backTo: string | null }) {
  const { timezone } = useWorkspace()
  const valuation = useApiQuery((client, signal) => client.valuation(valuationId, { signal }), [valuationId])
  const setId = comparableSetId ?? valuation.data?.comparable?.comparable_set_id ?? null
  return (
    <div className="screen">
      <p>
        {backTo ? <Link to={backTo}>← Candidate</Link> : null}
        {!backTo && valuation.data ? <Link to={`/candidates/${valuation.data.listing_id}`}>← Candidate</Link> : null}
      </p>
      <h1>Economics</h1>
      {valuation.status === 'loading' ? <LoadingState label="Loading valuation" /> : null}
      {valuation.error ? <ErrorPanel error={valuation.error} onRetry={valuation.reload} /> : null}
      {valuation.data && valuation.envelope ? (
        <>
          <ViewMeta
            asOf={valuation.envelope.as_of}
            fetchedAt={valuation.fetchedAt}
            timeZone={timezone}
            onReload={valuation.reload}
            reloading={valuation.reloading}
          />
          <Warnings warnings={valuation.envelope.warnings} />
          <ValuationBody valuation={valuation.data} timeZone={timezone} />
        </>
      ) : null}
      {setId ? <ComparablesPanel setId={setId} /> : null}
    </div>
  )
}

function ValuationBody({ valuation, timeZone }: { valuation: ValuationView; timeZone: string }) {
  return (
    <>
      <div className="badges">
        <Badge value={valuation.state} />
        {valuation.is_fixture ? <Badge tone="muted">{valuation.fixture_label ?? 'synthetic fixture'}</Badge> : null}
        {valuation.research_candidate ? <Badge tone="warn">research candidate</Badge> : null}
        {valuation.alert_eligible ? <Badge tone="ok">alert eligible</Badge> : <Badge tone="muted">not alert eligible</Badge>}
      </div>
      <Notice tone="info">{valuation.terminology_note}</Notice>
      {valuation.stale_at ? (
        <Notice tone="warn">
          This valuation is stale since <Timestamp value={valuation.stale_at} timeZone={timeZone} />
          {valuation.stale_reason ? `: ${valuation.stale_reason}` : ''}. A recalculation is needed before acting.
        </Notice>
      ) : null}

      <Section title={`Contribution (${CONTRIBUTION_LABEL})`} id="contribution">
        <p className="muted small">
          Figures come from the backend calculation; this page does not compute tax, costs or contributions. Unknown items are
          shown as &quot;unknown&quot;, never as EUR 0.00.
        </p>
        <KeyValues
          items={[
            [`Conservative ${CONTRIBUTION_LABEL}`, <Amount key="c" value={valuation.contributions.conservative} showReason />],
            [`Base ${CONTRIBUTION_LABEL}`, <Amount key="b" value={valuation.contributions.base} showReason />],
            [`Upside ${CONTRIBUTION_LABEL}`, <Amount key="u" value={valuation.contributions.upside} showReason />],
            ['Payable in EUR', <Amount key="p" value={valuation.payable_eur} showReason />],
            ['Material support', label(valuation.material_support)],
          ]}
        />
        {valuation.threshold ? (
          <div className="threshold" data-testid="threshold">
            <p>
              Minimum contribution threshold <Amount value={valuation.threshold.threshold} />{' '}
              <Badge value={valuation.threshold.label} tone={valuation.threshold.label === 'PROPOSED' ? 'warn' : 'ok'}>
                {valuation.threshold.label}
              </Badge>
              {valuation.threshold.label === 'PROPOSED' ? (
                <span className="muted"> (not owner-approved; for orientation only)</span>
              ) : null}
            </p>
            <p>
              Would meet the threshold: {valuation.threshold.would_meet === null ? 'unknown' : valuation.threshold.would_meet ? 'yes' : 'no'}
              {valuation.threshold.would_meet_by_scenario.length ? (
                <span className="muted">
                  {' '}
                  ({valuation.threshold.would_meet_by_scenario
                    .map((check) => `${check.scenario}: ${check.would_meet === null ? 'unknown' : check.would_meet ? 'yes' : 'no'}`)
                    .join(', ')})
                </span>
              ) : null}
            </p>
            {valuation.threshold.blockers.length ? (
              <p className="muted">Alert blockers: {valuation.threshold.blockers.map(label).join(', ')}</p>
            ) : null}
          </div>
        ) : null}
        {valuation.unknowns.length ? (
          <p>
            <strong>Unknown inputs:</strong> {valuation.unknowns.map(label).join(', ')}
          </p>
        ) : null}
      </Section>

      <Section title="Scenarios" id="scenarios">
        {valuation.scenarios.length === 0 ? (
          <EmptyState>No scenarios were calculated (missing inputs).</EmptyState>
        ) : (
          <div className="scenario-grid">
            {valuation.scenarios.map((scenario) => (
              <ScenarioCard key={scenario.scenario} scenario={scenario} />
            ))}
          </div>
        )}
      </Section>

      <Section title="Cost lines" id="cost-lines">
        {valuation.purchase ? (
          <p>
            {valuation.purchase.label}: <Amount value={valuation.purchase.amount} showReason /> <StatusLabel status={valuation.purchase.status} />
          </p>
        ) : null}
        {valuation.cost_lines.length === 0 ? (
          <EmptyState>No cost lines.</EmptyState>
        ) : (
          <table className="responsive-table" aria-label="Cost lines">
            <thead>
              <tr>
                <th scope="col">Item</th>
                <th scope="col">Status</th>
                <th scope="col">Low</th>
                <th scope="col">Base</th>
                <th scope="col">High</th>
                <th scope="col">Basis</th>
              </tr>
            </thead>
            <tbody>
              {valuation.cost_lines.map((line, index) => (
                <CostLineRow key={`${line.category}-${index}`} line={line} timeZone={timeZone} />
              ))}
            </tbody>
          </table>
        )}
        {valuation.proceeds ? (
          <div>
            <h3>Expected resale proceeds</h3>
            <p>
              {valuation.proceeds.label} <StatusLabel status={valuation.proceeds.status} />: low <Amount value={valuation.proceeds.low} /> · base{' '}
              <Amount value={valuation.proceeds.base} /> · high <Amount value={valuation.proceeds.high} />
            </p>
            <p className="muted small">
              {valuation.proceeds.notice} Evidence kind: {label(valuation.proceeds.evidence_kind)}; negotiation discount{' '}
              {valuation.proceeds.negotiation_discount_pct ?? 'unknown'} ({label(valuation.proceeds.discount_status)}).
            </p>
          </div>
        ) : null}
      </Section>

      {valuation.tax ? (
        <Section title="Import tax" id="tax">
          <p>
            Rule set {valuation.tax.rule_set_id} v{valuation.tax.version}{' '}
            <Badge value={valuation.tax.rule_status} tone={valuation.tax.production_ready ? 'ok' : 'warn'} />
          </p>
          <p className="muted">{valuation.tax.approval_label}</p>
          <p>
            {valuation.tax.complete ? (
              <>
                Total import cost: <Amount value={valuation.tax.total_import_cost} />
              </>
            ) : (
              <>
                Known subtotal (not a total): <Amount value={valuation.tax.known_subtotal} /> · unknown components:{' '}
                {valuation.tax.unknown_components.join(', ') || 'none'}
              </>
            )}
          </p>
          <ul className="item-list">
            {valuation.tax.components.map((component) => (
              <li key={component.component_id}>
                {component.label}: <Amount value={component.amount} showReason /> <span className="muted">({label(component.status)})</span>
                {component.missing_inputs.length ? <span className="muted"> · missing {component.missing_inputs.join(', ')}</span> : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : (
        <Section title="Import tax" id="tax">
          <p>
            Import tax: <span className="amount amount-unknown">unknown</span> (no approved, applicable rule set).
          </p>
        </Section>
      )}

      <Section title="Versions and dependencies" id="versions">
        <KeyValues
          items={[
            ['Calculation version', valuation.versions.calculation_version],
            ['Cost model version', valuation.versions.cost_model_version ?? 'none'],
            ['Tax engine version', valuation.versions.tax_engine_version ?? 'none'],
            ['Tax rule', valuation.versions.tax_rule ?? 'none'],
            ['Cost profile', valuation.versions.cost_profile ?? 'none'],
            ['Configuration revision', <code key="c">{valuation.versions.config_revision_id}</code>],
            ['Listing revision', valuation.listing_revision?.toString() ?? 'unknown'],
            ['Dependency fingerprint', <code key="f" className="wrap">{valuation.dependency_fingerprint}</code>],
            ['Created', <Timestamp key="t" value={valuation.created_at} timeZone={timeZone} />],
            ['Expires', <Timestamp key="x" value={valuation.expires_at} timeZone={timeZone} />],
          ]}
        />
        {valuation.assumptions.length ? (
          <>
            <h3>Assumptions</h3>
            <ul>
              {valuation.assumptions.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </>
        ) : null}
        {valuation.warnings.length ? (
          <>
            <h3>Valuation warnings</h3>
            <ul>
              {valuation.warnings.map((item, index) => (
                <li key={index}>{item}</li>
              ))}
            </ul>
          </>
        ) : null}
      </Section>
    </>
  )
}

function StatusLabel({ status }: { status: string }) {
  return <span className={`status-label status-${status}`}>{label(status)}</span>
}

function CostLineRow({ line, timeZone }: { line: CostLineView; timeZone: string }) {
  const notApplicable = line.status === 'not_applicable'
  // An unknown line is "unknown" whatever its bounds say: it is never shown as an amount (or 0.00).
  const unknown = line.status === 'unknown'
  const cell = (value: string | null) =>
    notApplicable ? (
      <span className="muted">not applicable</span>
    ) : unknown || value === null ? (
      <span className="amount amount-unknown">unknown</span>
    ) : (
      decimalText(value, line.currency)
    )
  return (
    <tr>
      <td data-label="Item">
        {line.label}
        <div className="muted small">{label(line.category)}</div>
      </td>
      <td data-label="Status">
        <StatusLabel status={line.status} />
        {line.declared_status !== line.status ? <div className="muted small">declared {label(line.declared_status)}</div> : null}
      </td>
      <td data-label="Low">{cell(line.low)}</td>
      <td data-label="Base">{cell(line.base)}</td>
      <td data-label="High">{cell(line.high)}</td>
      <td data-label="Basis">
        {line.reason ? <span>{line.reason}</span> : null}
        {line.provider ? <div className="muted small">provider {line.provider}</div> : null}
        {line.expires_at ? (
          <div className="muted small">
            expires <Timestamp value={line.expires_at} timeZone={timeZone} />
          </div>
        ) : null}
        {line.assumption_approved ? <div className="muted small">approved assumption</div> : null}
      </td>
    </tr>
  )
}

function ScenarioCard({ scenario }: { scenario: ScenarioView }) {
  return (
    <article className="card scenario" aria-label={`${scenario.scenario} scenario`}>
      <h3>
        {label(scenario.scenario)} scenario {scenario.complete ? <Badge tone="ok">complete</Badge> : <Badge tone="warn">incomplete</Badge>}
      </h3>
      <p>
        {scenario.proceeds_label}: <Amount value={scenario.expected_realized_proceeds} showReason />
      </p>
      <ul className="term-list">
        {scenario.components.map((term) => (
          <li key={term.term}>
            {term.label}: <Amount value={term.amount} showReason />
          </li>
        ))}
      </ul>
      {scenario.complete && scenario.totals ? (
        <ul className="term-list totals">
          {scenario.totals.map((term) => (
            <li key={term.term}>
              <strong>{term.label}</strong>: <Amount value={term.amount} />
            </li>
          ))}
        </ul>
      ) : (
        <p>
          Known subtotal (not a total): <Amount value={scenario.known_subtotal} />
        </p>
      )}
      {scenario.unknown_lines.length ? (
        <div>
          <p className="panel-title">Unknown lines</p>
          <ul>
            {scenario.unknown_lines.map((line) => (
              <li key={line.item}>
                {line.label}: <span className="amount amount-unknown">unknown</span> <span className="muted">({line.reason})</span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <p>
        {scenario.contribution_label}: <Amount value={scenario.contribution_before_business_tax} showReason />
      </p>
    </article>
  )
}

function ComparablesPanel({ setId }: { setId: string }) {
  const [includeExcluded, setIncludeExcluded] = useState(false)
  const comparables = useApiQuery(
    (client, signal) => client.comparables(setId, { include_excluded: includeExcluded, limit: 50 }, { signal }),
    [setId, includeExcluded],
  )
  return (
    <Section
      title="MK comparables"
      id="comparables"
      actions={
        <label className="inline-check">
          <input type="checkbox" checked={includeExcluded} onChange={(event) => setIncludeExcluded(event.target.checked)} /> Include
          excluded evidence
        </label>
      }
    >
      {comparables.status === 'loading' ? <LoadingState label="Loading comparables" /> : null}
      {comparables.error ? <ErrorPanel error={comparables.error} onRetry={comparables.reload} /> : null}
      {comparables.data ? <ComparablesBody set={comparables.data} /> : null}
    </Section>
  )
}

function StatsTable({ title, stats }: { title: string; stats: EvidenceStatsView | null }) {
  return (
    <div className="stats">
      <h3>{title}</h3>
      {stats ? (
        <>
          <p className="muted small">{stats.evidence_note}</p>
          <KeyValues
            items={[
              ['Observations', `${stats.n} (${label(stats.sample_label)})`],
              ['Median (EUR)', groupDecimal(stats.median)],
              ['Range (EUR)', `${groupDecimal(stats.min)} – ${groupDecimal(stats.max)}`],
              ['Quartiles (EUR)', stats.q1 && stats.q3 ? `${groupDecimal(stats.q1)} – ${groupDecimal(stats.q3)}` : 'unknown'],
            ]}
          />
        </>
      ) : (
        <p className="muted">No evidence of this kind.</p>
      )}
    </div>
  )
}

function ComparablesBody({ set }: { set: ComparableSetView }) {
  return (
    <>
      <div className="badges">
        <Badge value={set.status} tone={set.status === 'adequate' ? 'ok' : 'warn'} />
        {set.research_needed ? <Badge tone="warn">research needed</Badge> : null}
        {set.is_fixture ? <Badge tone="muted">synthetic fixture</Badge> : null}
      </div>
      <Notice tone="info">{set.asking_vs_sale_notice}</Notice>
      <p>
        MK resale band EUR {groupDecimal(set.mk_band.min_eur)} – {groupDecimal(set.mk_band.max_eur)}: fit {label(set.mk_band.fit)}{' '}
        <span className="muted">({set.mk_band.meaning})</span>
      </p>
      <div className="stats-grid">
        <StatsTable title="Asking prices (advertised, not sales)" stats={set.asking_price_stats} />
        <StatsTable title="Seller-reported sales (unverified)" stats={set.seller_reported_sale_stats} />
        <StatsTable title="Verified sales" stats={set.verified_sale_stats} />
      </div>
      <p className="muted">
        Selected {set.selected_count} · excluded {set.excluded_count} · criteria {set.criteria_version}
      </p>
      {set.members.length === 0 ? (
        <EmptyState>No comparable members on this page.</EmptyState>
      ) : (
        <table className="responsive-table" aria-label="Comparable members">
          <thead>
            <tr>
              <th scope="col">Vehicle</th>
              <th scope="col">Role</th>
              <th scope="col">Evidence kind</th>
              <th scope="col">Advertised</th>
              <th scope="col">EUR</th>
              <th scope="col">Match</th>
            </tr>
          </thead>
          <tbody>
            {set.members.map((member) => (
              <tr key={member.observation_id}>
                <td data-label="Vehicle">
                  {[member.make, member.model, member.generation].filter(Boolean).join(' ') || 'unknown'} · {kmText(member.mileage_km)}
                  {member.url ? (
                    <div>
                      <ExternalLink href={member.url}>advert</ExternalLink>
                    </div>
                  ) : null}
                </td>
                <td data-label="Role">
                  <Badge value={member.role} tone={member.role === 'selected' ? 'ok' : 'muted'} />
                  {member.exclusion_reasons.length ? <div className="muted small">{member.exclusion_reasons.map(label).join(', ')}</div> : null}
                </td>
                <td data-label="Evidence kind">
                  {label(member.evidence_kind)}
                  <div className="muted small">{member.evidence_note}</div>
                </td>
                <td data-label="Advertised">
                  <Amount value={member.advertised} />
                </td>
                <td data-label="EUR">
                  <Amount value={member.amount_eur} />
                </td>
                <td data-label="Match">
                  {label(member.match_level)}
                  {member.differences.length ? (
                    <div className="muted small">{member.differences.map((d) => `${d.dimension}: ${label(d.code)}`).join('; ')}</div>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}
