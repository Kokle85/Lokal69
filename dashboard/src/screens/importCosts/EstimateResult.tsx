/**
 * One import-cost estimate as the server computed it (`ImportCostEstimateView`). Display only: no
 * figure is derived in the browser; unknown amounts read "unknown" (never 0).
 *
 * - An INDICATIVE PREVIEW (unapproved rules) is labelled prominently with the server's MK and EN
 *   banner, and its totals are named "indicative"; `unavailable` says that every import charge is
 *   unknown; only `active` rules give a "total".
 * - Helper components (customs value in MKD, the VAT base, ...) are shown as secondary rows: they are
 *   never counted in a total.
 */
import type { ReactNode } from 'react'
import { Link } from 'react-router'
import type {
  AmountView,
  ImportCostEstimateView,
  ImportFactsView,
  ImportScenarioView,
  ResponseWarning,
} from '../../api/types'
import { Amount, Badge, KeyValues, Notice, Section, Warnings } from '../../components/ui'
import { label, shortHash, yesNo } from '../../format'

const FX_SOURCE_TEXT: Record<ImportCostEstimateView['customs_fx_source'], string> = {
  recorded: 'recorded Customs monthly list rate',
  caller_supplied: 'rate you entered (used for this estimate only, never stored)',
  not_needed: 'not needed (the rules are in EUR)',
  none: 'none: the MKD amounts that depend on it are unknown',
}

const SCENARIO_TITLES: Record<ImportScenarioView['name'], string> = {
  legal_base: 'Legal base',
  standard_duty_if_proof_rejected: 'Alternative: proof of origin rejected (standard duty)',
  customs_revaluation_risk: 'Risk: customs revaluation to the catalogue value',
}

function TextList({ items, label: name, className = 'item-list small' }: { items: string[]; label: string; className?: string }) {
  if (!items.length) return <p className="muted small">none</p>
  return (
    <ul className={className} aria-label={name}>
      {items.map((item, index) => (
        <li key={index}>{item}</li>
      ))}
    </ul>
  )
}

/** The prominent label of a preview (or of an unavailable result): the server's MK and EN text. */
export function EstimateBanner({ view }: { view: ImportCostEstimateView }) {
  if (view.mode === 'active') {
    return (
      <Notice tone="ok">
        <p>
          <strong>Owner-approved ACTIVE rules</strong> ({view.rule?.rule_set_id}@{view.rule?.version}): the same rules
          valuations use. Still an estimate, not a customs assessment.
        </p>
      </Notice>
    )
  }
  const preview = view.mode === 'indicative_preview'
  return (
    <div
      className={preview ? 'estimate-banner estimate-banner-preview' : 'estimate-banner estimate-banner-unavailable'}
      role="note"
      aria-label={preview ? 'Indicative preview' : 'Import charges unavailable'}
      data-testid="estimate-banner"
    >
      <p className="estimate-banner-title">
        {preview ? 'ИНДИКАТИВНА ПРОЦЕНКА · INDICATIVE PREVIEW' : 'НЕДОСТАПНО · UNAVAILABLE'}
      </p>
      {view.banner ? (
        <>
          <p lang="mk">{view.banner.mk}</p>
          <p lang="en">{view.banner.en}</p>
        </>
      ) : null}
      {!preview ? <p>{view.selection_reason}</p> : null}
    </div>
  )
}

function transportText(assumptions: string[]): string | null {
  const line = assumptions.find((item) => item.startsWith('TRANSPORT_LINE:'))
  return line ? line.slice('TRANSPORT_LINE:'.length) : null
}

function Totals({ scenario, preview, view }: { scenario: ImportScenarioView; preview: boolean; view: ImportCostEstimateView }) {
  const word = preview ? 'Indicative total' : 'Total'
  const total = preview ? scenario.indicative_total : scenario.total
  const totalEur = preview ? scenario.indicative_total_eur : scenario.total_eur
  const landed = preview ? scenario.indicative_landed_cost_eur : scenario.landed_cost_eur
  const items: Array<[string, ReactNode]> = [
    [`${word} import charges (${view.currency ?? 'rule currency'})`, <Amount key="t" value={total} showReason />],
    [`${word} import charges (EUR at the customs rate)`, <Amount key="te" value={totalEur} showReason />],
    [
      `${preview ? 'Indicative landed cost' : 'Landed cost'} (EUR)`,
      <span key="l">
        <Amount value={landed} showReason /> <span className="muted small">{view.landed_cost_note}</span>
      </span>,
    ],
  ]
  if (scenario.known_subtotal) {
    items.push([
      'Known subtotal (not a total)',
      <span key="k">
        <Amount value={scenario.known_subtotal} /> <span className="muted small">only the charges that resolved</span>
      </span>,
    ])
  }
  items.push(['Customs value', <Amount key="cv" value={scenario.customs_value} showReason />])
  if (scenario.customs_value_basis) items.push(['Customs value basis', scenario.customs_value_basis])
  return (
    <div className="estimate-totals" data-testid={`totals-${scenario.name}`}>
      <KeyValues items={items} />
    </div>
  )
}

function amountCell(value: AmountView) {
  return <Amount value={value} showReason />
}

function ScenarioSection({ scenario, view }: { scenario: ImportScenarioView; view: ImportCostEstimateView }) {
  const preview = view.mode === 'indicative_preview'
  return (
    <Section title={SCENARIO_TITLES[scenario.name]} id={`scenario-${scenario.name}`}>
      <p className="muted small">
        {scenario.label} <Badge value={scenario.complete ? 'resolved' : 'incomplete'}>{scenario.complete ? 'complete' : 'incomplete'}</Badge>{' '}
        {preview ? <Badge tone="warn">indicative</Badge> : null}
      </p>
      <Totals scenario={scenario} preview={preview} view={view} />
      {!scenario.complete ? (
        <Notice tone="warn">
          <p>
            Incomplete: unknown {scenario.unknown_components.join(', ') || 'components'}
            {scenario.missing_inputs.length ? `; missing ${scenario.missing_inputs.join(', ')}` : ''}. Unknown charges are
            never counted as zero.
          </p>
        </Notice>
      ) : null}
      <table className="responsive-table" aria-label={`Import charges by category (${scenario.name})`}>
        <thead>
          <tr>
            <th scope="col">Category</th>
            <th scope="col">Status</th>
            <th scope="col">Amount ({view.currency ?? 'rule currency'})</th>
            <th scope="col">EUR at the customs rate</th>
          </tr>
        </thead>
        <tbody>
          {scenario.categories.map((row) => (
            <tr key={row.category}>
              <td data-label="Category">{label(row.category)}</td>
              <td data-label="Status">
                <Badge value={row.status} />
              </td>
              <td data-label="Amount">{amountCell(row.amount)}</td>
              <td data-label="EUR">{amountCell(row.amount_eur)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <table className="responsive-table" aria-label={`Components (${scenario.name})`}>
        <thead>
          <tr>
            <th scope="col">Component</th>
            <th scope="col">Category</th>
            <th scope="col">Status</th>
            <th scope="col">Amount</th>
          </tr>
        </thead>
        <tbody>
          {scenario.components.map((component) => (
            <tr key={component.component_id} className={component.intermediate ? 'row-intermediate' : undefined}>
              <td data-label="Component">
                {component.label} <code className="muted">{component.component_id}</code>
                {component.intermediate ? (
                  <>
                    {' '}
                    <Badge tone="muted">intermediate · not counted in the total</Badge>
                  </>
                ) : null}
                {component.missing_inputs.length ? (
                  <span className="muted small"> · missing {component.missing_inputs.join(', ')}</span>
                ) : null}
                {component.warnings.length ? <span className="muted small"> · {component.warnings.join('; ')}</span> : null}
                {component.inputs_used.length ? (
                  <details className="small">
                    <summary>Inputs used</summary>
                    <ul className="plain-list">
                      {component.inputs_used.map((input, index) => (
                        <li key={index}>
                          <code>{input.name}</code>: {input.value}
                        </li>
                      ))}
                    </ul>
                  </details>
                ) : null}
              </td>
              <td data-label="Category">{label(component.category)}</td>
              <td data-label="Status">
                <Badge value={component.status} />
              </td>
              <td data-label="Amount">{amountCell(component.amount)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {scenario.assumptions.length ? (
        <>
          <h3>Assumptions of this scenario</h3>
          <TextList items={scenario.assumptions} label={`Assumptions (${scenario.name})`} />
        </>
      ) : null}
      {scenario.warnings.length ? (
        <>
          <h3>Engine warnings</h3>
          <TextList items={scenario.warnings} label={`Engine warnings (${scenario.name})`} />
        </>
      ) : null}
      <p className="muted small">
        Engine {scenario.engine_version} · effective {scenario.effective_date} · calculation{' '}
        <code title={scenario.calculation_sha256}>{shortHash(scenario.calculation_sha256)}</code>
      </p>
    </Section>
  )
}

const FACT_LABELS: Record<string, string> = {
  price: 'Purchase price',
  price_basis: 'Price basis',
  declaration_date: 'Declaration date',
  fuel: 'Fuel / drive',
  co2: 'CO2',
  co2_document: 'CO2 document',
  co2_round_up_confirmed: 'CO2 round-up confirmed',
  first_registration: 'First registration',
  emissions_class: 'Euro class',
  origin_proof: 'Proof of origin',
  hs_code: 'Tariff code',
  hs_evidence_ref: 'Tariff code evidence',
  transport_after_entry_eur: 'Inland transport after entry (EUR)',
  transport_after_entry_separately_invoiced: 'Inland leg separately invoiced',
  catalogue_value_eur: 'Catalogue value (EUR)',
  importer_status: 'Importer',
  mileage_km: 'Mileage (km)',
  seller_country: 'Seller country',
  engine_displacement_cm3: 'Engine (cm3)',
  power_kw: 'Power (kW)',
}

function given(value: string | null | undefined): ReactNode {
  return value ? value : <span className="muted">not given</span>
}

function factValue(fact: string, facts: ImportFactsView): ReactNode {
  const text = given
  switch (fact) {
    case 'price':
      return <Amount value={facts.price} showReason />
    case 'price_basis':
      return label(facts.price_basis)
    case 'declaration_date':
      return text(facts.declaration_date)
    case 'fuel':
      return label(facts.fuel)
    case 'co2':
      return facts.co2.length ? facts.co2.map((r) => `${r.g_km} g/km (${label(r.cycle)})`).join(', ') : text(null)
    case 'co2_document':
      return facts.co2_document ? label(facts.co2_document) : text(null)
    case 'co2_round_up_confirmed':
      return yesNo(facts.co2_round_up_confirmed)
    case 'origin_proof': {
      const parts = [label(facts.origin_proof_kind), facts.origin_proof_country, facts.origin_proof_evidence_ref]
      return parts.filter(Boolean).join(' · ')
    }
    case 'transport_after_entry_separately_invoiced':
      return yesNo(facts.transport_after_entry_separately_invoiced)
    case 'importer_status':
      return facts.importer_status ? label(facts.importer_status) : text(null)
    default: {
      const value = (facts as unknown as Record<string, string | null>)[fact]
      return text(typeof value === 'string' ? value : null)
    }
  }
}

function FactsTable({ view }: { view: ImportCostEstimateView }) {
  return (
    <table className="responsive-table" aria-label="Facts used">
      <thead>
        <tr>
          <th scope="col">Fact</th>
          <th scope="col">Value used</th>
          <th scope="col">Source</th>
        </tr>
      </thead>
      <tbody>
        {view.fact_sources.map(({ fact, source }) => (
          <tr key={fact}>
            <td data-label="Fact">{FACT_LABELS[fact] ?? label(fact)}</td>
            <td data-label="Value used">{factValue(fact, view.facts)}</td>
            <td data-label="Source">
              <Badge tone={source === 'request' ? 'info' : source === 'listing' ? 'neutral' : 'muted'}>
                {source === 'request' ? 'you' : source === 'listing' ? 'listing' : 'default'}
              </Badge>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function EstimateResult({ view, warnings }: { view: ImportCostEstimateView; warnings: ResponseWarning[] }) {
  const transport = transportText(view.assumptions)
  const uncertain = [...view.uncertain_items].sort((a, b) => Number(b.applies) - Number(a.applies))
  return (
    <div className="estimate" data-testid="estimate" data-mode={view.mode}>
      <EstimateBanner view={view} />
      <p className="muted small">{view.usage_note}</p>
      <Warnings warnings={warnings} />
      {view.listing?.is_fixture ? <Notice tone="warn">{view.listing.fixture_label}</Notice> : null}
      {view.missing_facts.length ? (
        <Notice tone="warn">
          <p>
            Needed to complete the calculation: <strong>{view.missing_facts.map(label).join(', ')}</strong>
          </p>
        </Notice>
      ) : null}
      {view.unverified_inputs.length ? (
        <Notice tone="warn">
          <p>The figures rest on unverified claims:</p>
          <TextList items={view.unverified_inputs} label="Unverified inputs" />
        </Notice>
      ) : null}

      <Section title="Basis" id="estimate-basis">
        <KeyValues
          items={[
            [
              'Rules',
              view.rule ? (
                <span key="r">
                  {view.rule.rule_set_id}@{view.rule.version} <Badge value={view.rule.status} />{' '}
                  <code className="muted" title={view.rule.content_sha256}>
                    {view.rule.sha256_recorded ? 'sha256' : 'content hash'} {shortHash(view.rule.content_sha256)}
                  </code>
                </span>
              ) : (
                <span key="rn" className="muted">
                  no rule set applies
                </span>
              ),
            ],
            ['Mode', <Badge key="m" tone={view.mode === 'active' ? 'ok' : view.mode === 'unavailable' ? 'bad' : 'warn'}>{label(view.mode)}</Badge>],
            ['Production-ready', yesNo(view.production_ready)],
            ['Jurisdiction / category', `${view.jurisdiction} · ${label(view.vehicle_category)}`],
            ['Declaration date', view.declaration_date],
            [
              'Customs rate',
              <span key="fx">
                {view.customs_fx_rate
                  ? `${view.customs_fx_rate.direction} (${view.customs_fx_rate.provider}, ${view.customs_fx_rate.rate_date})`
                  : 'none'}{' '}
                <span className="muted small">· {FX_SOURCE_TEXT[view.customs_fx_source]}</span>
              </span>,
            ],
            ['EUR equivalents', view.eur_basis],
            [
              'Transport (cost profile)',
              transport ?? (
                <span key="tr" className="muted">
                  see the assumptions
                </span>
              ),
            ],
            ['Selection', view.selection_reason],
          ]}
        />
        {view.listing ? (
          <p className="small">
            Facts from listing <Link to={`/candidates/${view.listing.listing_id}`}>{view.listing.source_key}</Link>, revision{' '}
            {view.listing.revision_number}
            {view.listing.screened ? '' : ' (not screened yet: the price is unknown)'}.
          </p>
        ) : null}
      </Section>

      <Section title="Import gate (Euro class)" id="estimate-gate">
        <KeyValues
          items={[
            ['Importable', <Badge key="i" tone={view.gates.importable === 'yes' ? 'ok' : view.gates.importable === 'no' ? 'bad' : 'warn'}>{view.gates.importable}</Badge>],
            ['Reason', view.gates.reason],
            ['Euro class', view.gates.euro_class === null ? 'unknown' : `Euro ${view.gates.euro_class} (minimum Euro ${view.gates.min_euro})`],
            ['Legal basis', view.gates.legal_basis],
          ]}
        />
        <p className="muted small">The gate never changes an amount; the calculation runs either way.</p>
      </Section>

      {view.scenarios.map((scenario) => (
        <ScenarioSection key={scenario.name} scenario={scenario} view={view} />
      ))}

      <Section title="Facts used" id="estimate-facts">
        <FactsTable view={view} />
      </Section>

      <Section title="Assumptions" id="estimate-assumptions">
        <TextList items={view.assumptions} label="Assumptions" />
      </Section>

      <Section title="Warnings and notes" id="estimate-warnings">
        <TextList items={[...view.warnings, ...view.notes]} label="Estimate warnings" />
      </Section>

      <Section title="To confirm before approval (UNCERTAIN)" id="estimate-uncertain">
        {uncertain.length ? (
          <ul className="item-list small" aria-label="Uncertain items">
            {uncertain.map((item, index) => (
              <li key={`${item.component_id ?? 'rule_set'}-${index}`}>
                {item.applies ? <Badge tone="warn">applies</Badge> : <Badge tone="muted">not this vehicle</Badge>}{' '}
                {item.component_id ? <code>{item.component_id}</code> : null} {item.text}
              </li>
            ))}
          </ul>
        ) : (
          <p className="muted small">none</p>
        )}
        {view.considered.length ? (
          <details className="small">
            <summary>Rule sets considered</summary>
            <TextList items={view.considered} label="Rule sets considered" />
          </details>
        ) : null}
        <p className="muted small">Estimator {view.estimator_version}</p>
      </Section>
    </div>
  )
}
