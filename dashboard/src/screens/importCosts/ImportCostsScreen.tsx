/**
 * Customs calculator (`/import-costs`, and `/candidates/:listingId/import-costs` prefilled from that
 * listing): North Macedonian customs duty, motor-vehicle tax (ДМВ) and import VAT for one vehicle,
 * plus the landed cost with the fixed per-vehicle transport of the cost profile.
 *
 * `GET /api/import-costs/estimate` (read-only; the same calculation dot gets through MCP). The URL
 * holds the submitted parameters, so a calculation can be reloaded and navigated back to. Every
 * amount is the server's; until the owner approves and activates a rule set the result is an
 * INDICATIVE PREVIEW on unapproved draft rules, labelled as such and never stored anywhere.
 */
import { Link, useParams, useSearchParams } from 'react-router'
import type { ImportCostEstimateQuery } from '../../api/types'
import { EmptyState, ErrorPanel, LoadingState, Section, ViewMeta } from '../../components/ui'
import { useApiQuery } from '../../hooks/useApiQuery'
import { useWorkspace } from '../../workspace/WorkspaceProvider'
import { CalculatorForm } from './CalculatorForm'
import { EstimateResult } from './EstimateResult'
import { FIELD_LABELS, formFromSearch, invalidKeys, listingHints, queryFromSearch, searchFromForm, type FormState } from './form'

interface EstimateProps {
  query: ImportCostEstimateQuery
  queryKey: string
  initial: FormState
  listingMode: boolean
  onSubmit(form: FormState): void
}

/** The form plus one estimate of the URL's parameters (loaded on mount and on every new URL). */
function Estimate({ query, queryKey, initial, listingMode, onSubmit }: EstimateProps) {
  const { timezone } = useWorkspace()
  const estimate = useApiQuery((api, signal) => api.importCostEstimate(query, { signal }), [queryKey])
  const invalid = estimate.error?.code === 'VALIDATION_ERROR' ? invalidKeys(estimate.error.fields) : new Set<string>()
  const busy = estimate.status === 'loading' || estimate.reloading
  return (
    <>
      <Section title="Vehicle facts" id="calculator-form">
        <CalculatorForm
          key={queryKey}
          initial={initial}
          listingMode={listingMode}
          hints={listingHints(estimate.data)}
          invalid={invalid}
          busy={busy}
          onSubmit={onSubmit}
        />
      </Section>
      <div aria-live="polite">
        {estimate.status === 'loading' ? <LoadingState label="Calculating the import charges" /> : null}
        {estimate.error ? (
          <>
            <ErrorPanel error={estimate.error} onRetry={estimate.reload} />
            {invalid.size ? (
              <p className="form-error" role="alert">
                Check: {[...invalid].map((key) => FIELD_LABELS[key as keyof typeof FIELD_LABELS]).join(', ')}
              </p>
            ) : null}
          </>
        ) : null}
      </div>
      {estimate.data && estimate.envelope && !estimate.error ? (
        <>
          <ViewMeta
            asOf={estimate.envelope.as_of}
            fetchedAt={estimate.fetchedAt}
            timeZone={timezone}
            onReload={estimate.reload}
            reloading={estimate.reloading}
          />
          <EstimateResult view={estimate.data} warnings={estimate.envelope.warnings} />
        </>
      ) : null}
    </>
  )
}

export function ImportCostsScreen() {
  const { listingId } = useParams()
  const [search, setSearch] = useSearchParams()
  const given = searchFromForm(formFromSearch(search))
  const queryKey = `${listingId ?? ''}?${given.toString()}`
  const runnable = listingId !== undefined || given.toString() !== ''
  const submit = (form: FormState) => setSearch(searchFromForm(form))
  const initial = formFromSearch(search)
  return (
    <div className="screen">
      {listingId ? (
        <p>
          <Link to={`/candidates/${listingId}`}>← Candidate detail</Link> · <Link to="/import-costs">Empty calculator</Link>
        </p>
      ) : null}
      <h1>Customs calculator</h1>
      <p className="muted">
        North Macedonia import charges for one passenger car: customs duty, motor-vehicle tax (ДМВ) and import VAT, and the
        landed cost (price + the fixed per-vehicle transport + these charges). Calculated by the server from the vehicle
        facts; nothing is stored and nothing here is a customs assessment.
      </p>
      {runnable ? (
        <Estimate
          key={listingId ?? 'standalone'}
          query={queryFromSearch(search, listingId)}
          queryKey={queryKey}
          initial={initial}
          listingMode={listingId !== undefined}
          onSubmit={submit}
        />
      ) : (
        <>
          <Section title="Vehicle facts" id="calculator-form">
            <CalculatorForm initial={initial} listingMode={false} hints={{}} invalid={new Set()} busy={false} onSubmit={submit} />
          </Section>
          <EmptyState>
            Fill in what you know about the vehicle and calculate. To start from a listing, open it under Candidates and choose
            &quot;Customs calculator&quot;.
          </EmptyState>
        </>
      )}
    </div>
  )
}
