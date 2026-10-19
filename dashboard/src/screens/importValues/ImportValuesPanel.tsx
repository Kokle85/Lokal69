/**
 * Candidate detail: the listing's recorded IMPORT VALUES (owner decision 2026-10-13, "two separate
 * values per vehicle"; `GET` / `POST /api/listings/{listing_id}/import-values`).
 *
 * - The REAL purchase price (реална набавна цена): what is, or will be, actually paid. Valuations
 *   use it for the profit and the landed cost instead of the advertised price.
 * - The ESTIMATED customs value (проценета царинска вредност): the value of the VEHICLE for
 *   customs in EUR, without transport (e.g. the SchwackeNet catalogue value). The import charges
 *   are computed on it plus the transport to the MK border; Customs first applies the price
 *   actually paid (Customs Law art. 28), which the calculator shows as the alternative.
 *
 * Everyone in the workspace sees the current values, their source and a short history (whole
 * euros, as the server sends them; "not recorded" is never shown as 0). Only the OWNER
 * (`config:admin`) records or updates them: one explicit request with an idempotency key
 * (`useIdempotentMutation`, same-key retry after an unknown outcome), confirmed after reading that
 * it changes this listing's valuation. The server validates every value again; nothing is computed
 * here.
 */
import { useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import {
  FORBIDDEN_TEXT_CHARS,
  type ImportValueRecordView,
  type ImportValuesRecordRequest,
  type ImportValuesRecordResult,
  type ImportValuesView,
  type RecordedValueKind,
} from '../../api/types'
import { Amount, Badge, EmptyState, ErrorPanel, LoadingState, Notice, Section, Timestamp } from '../../components/ui'
import { useApiQuery } from '../../hooks/useApiQuery'
import { MutationStatus } from '../../review/MutationStatus'
import { useIdempotentMutation } from '../../review/useIdempotentMutation'
import { useWorkspace } from '../../workspace/WorkspaceProvider'

/** `api.schemas.ImportValuesRecordRequest` limits (schemas/api/listings.listing_id.import-values.post.json). */
const AMOUNT_PATTERN = /^(0|[1-9][0-9]{0,12})(\.[0-9]{1,2})?$/
const ZERO_AMOUNT = /^0(\.0{1,2})?$/
const SOURCE_MIN = 3
const SOURCE_MAX = 120
const DOCUMENT_MAX = 200

const PURCHASE_TITLE = 'Purchase price (real, for profit)'
const PURCHASE_TITLE_MK = 'Реална набавна цена (за профит)'
const CUSTOMS_TITLE = 'Estimated customs value (for duties)'
const CUSTOMS_TITLE_MK = 'Проценета царинска вредност (за давачки)'

export const KIND_TEXT: Record<RecordedValueKind, string> = {
  actual: 'actual (paid)',
  quote: 'quote (offered or agreed, not paid yet)',
  estimate: 'estimate',
}

const RECORD_EFFECT =
  "Recording changes this listing's valuation: its current valuation is marked stale and recomputed with these values " +
  '(the purchase price for the profit and the landed cost, the estimated customs value for the import charges). A new value ' +
  'replaces the current one of its kind; the history keeps the old one.'

export function ImportValuesPanel({ listingId, onRecorded }: { listingId: string; onRecorded?: () => void }) {
  const { can, timezone } = useWorkspace()
  const values = useApiQuery((api, signal) => api.importValues(listingId, { signal }), [listingId])
  return (
    <Section title="Import values" id="import-values">
      <p className="muted small">
        Two separate values per vehicle · <span lang="mk">Две одделни вредности по возило</span>: the real purchase price (profit,
        landed cost) and the estimated customs value (import charges).
      </p>
      {values.status === 'loading' ? <LoadingState label="Loading the import values" /> : null}
      {values.error ? <ErrorPanel error={values.error} onRetry={values.reload} /> : null}
      {values.data ? <CurrentValues view={values.data} timeZone={timezone} /> : null}
      <p>
        <Link to={`/candidates/${listingId}/import-costs`} data-testid="import-values-calculator-link">
          Customs calculator for this listing
        </Link>{' '}
        <span className="muted small">(uses the recorded values unless you enter others there)</span>
      </p>
      {can('config:admin') ? (
        <RecordForm
          listingId={listingId}
          onRecorded={() => {
            values.reload()
            onRecorded?.()
          }}
        />
      ) : (
        <p className="muted small" data-testid="import-values-owner-only">
          Only the workspace owner records or updates these values.
        </p>
      )}
    </Section>
  )
}

function ValueCard({
  title,
  mk,
  record,
  empty,
  detail,
  testId,
  timeZone,
}: {
  title: string
  mk: string
  record: ImportValueRecordView | null
  empty: string
  detail: string
  testId: string
  timeZone: string
}) {
  return (
    <div className="card value-card" data-testid={testId}>
      <h3>
        {title} ·{' '}
        <span lang="mk" className="label-mk">
          {mk}
        </span>
      </h3>
      {record ? (
        <>
          <span className="value-amount">
            <Amount value={record.amount} showReason />
          </span>
          <span className="value-meta">
            {record.category === 'purchase' ? <>Kind: {KIND_TEXT[record.kind]} · </> : null}
            Source: {record.source}
            {record.document_ref ? <> · document {record.document_ref}</> : null}
          </span>
          <span className="value-meta">
            Recorded <Timestamp value={record.recorded_at} timeZone={timeZone} />
            {record.is_fixture ? (
              <>
                {' '}
                <Badge tone="muted">synthetic fixture</Badge>
              </>
            ) : null}
          </span>
        </>
      ) : (
        <span className="value-meta" data-testid={`${testId}-none`}>
          Not recorded. {empty}
        </span>
      )}
      <span className="value-meta">{detail}</span>
    </div>
  )
}

function CurrentValues({ view, timeZone }: { view: ImportValuesView; timeZone: string }) {
  return (
    <>
      <div className="value-pair" data-testid="import-values-current">
        <ValueCard
          title={PURCHASE_TITLE}
          mk={PURCHASE_TITLE_MK}
          record={view.purchase_price}
          empty="Valuations and the calculator use the advertised price."
          detail="The profit and every landed cost use it."
          testId="recorded-purchase-price"
          timeZone={timeZone}
        />
        <ValueCard
          title={CUSTOMS_TITLE}
          mk={CUSTOMS_TITLE_MK}
          record={view.estimated_customs_value}
          empty="The import charges use the price paid (transaction value)."
          detail="EUR, the vehicle without transport; the import charges use it plus the transport to the MK border."
          testId="recorded-customs-value"
          timeZone={timeZone}
        />
      </div>
      <p className="muted small">{view.note}</p>
      {view.history.length ? (
        <details className="small">
          <summary>History ({view.history.length})</summary>
          <table className="responsive-table" aria-label="Import value history">
            <thead>
              <tr>
                <th scope="col">Recorded</th>
                <th scope="col">Value</th>
                <th scope="col">Amount</th>
                <th scope="col">Kind</th>
                <th scope="col">Source</th>
                <th scope="col">Status</th>
              </tr>
            </thead>
            <tbody>
              {view.history.map((record) => (
                <tr key={record.id}>
                  <td data-label="Recorded">
                    <Timestamp value={record.recorded_at} timeZone={timeZone} />
                  </td>
                  <td data-label="Value">{record.category === 'purchase' ? 'purchase price' : 'estimated customs value'}</td>
                  <td data-label="Amount">
                    <Amount value={record.amount} showReason />
                  </td>
                  <td data-label="Kind">{KIND_TEXT[record.kind]}</td>
                  <td data-label="Source">
                    {record.source}
                    {record.document_ref ? <span className="muted"> · {record.document_ref}</span> : null}
                  </td>
                  <td data-label="Status">
                    {record.current ? <Badge tone="ok">current</Badge> : <Badge tone="muted">superseded</Badge>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      ) : (
        <EmptyState>Nothing recorded for this listing yet.</EmptyState>
      )}
    </>
  )
}

// ------------------------------------------------------------------------------- owner form

interface Draft {
  priceAmount: string
  priceKind: RecordedValueKind | ''
  priceSource: string
  priceDocument: string
  estimateAmount: string
  estimateSource: string
  estimateDocument: string
}

const EMPTY: Draft = {
  priceAmount: '',
  priceKind: '',
  priceSource: '',
  priceDocument: '',
  estimateAmount: '',
  estimateSource: '',
  estimateDocument: '',
}

/** The request field each input sends (to mark what a server-side validation error names). */
const FIELD_OF: Record<keyof Draft, string> = {
  priceAmount: 'purchase_price.amount',
  priceKind: 'purchase_price.kind',
  priceSource: 'purchase_price.source',
  priceDocument: 'purchase_price.document_ref',
  estimateAmount: 'estimated_customs_value.amount_eur',
  estimateSource: 'estimated_customs_value.source',
  estimateDocument: 'estimated_customs_value.document_ref',
}

function used(draft: Draft, group: 'price' | 'estimate'): boolean {
  if (group === 'price') {
    return [draft.priceAmount, draft.priceKind, draft.priceSource, draft.priceDocument].some((value) => value.trim() !== '')
  }
  return [draft.estimateAmount, draft.estimateSource, draft.estimateDocument].some((value) => value.trim() !== '')
}

function amountProblem(value: string, name: string): string | null {
  const amount = value.trim()
  if (!amount) return `Enter the ${name} in EUR.`
  if (!AMOUNT_PATTERN.test(amount)) return `The ${name} is a plain amount in EUR with a dot and at most two decimals (e.g. 2800.00).`
  if (ZERO_AMOUNT.test(amount)) return `The ${name} is more than zero (a value that is not known is simply not recorded).`
  return null
}

function textProblem(value: string, name: string, required: boolean, min: number, max: number): string | null {
  const text = value.trim()
  if (!text) return required ? `Name the ${name}.` : null
  if (text.length < min || text.length > max) return `The ${name} has ${min} to ${max} characters.`
  if (FORBIDDEN_TEXT_CHARS.test(text)) return `The ${name} contains control or direction-changing characters.`
  return null
}

/** Client-side checks of a draft (the server checks everything again). */
export function draftProblems(draft: Draft): string[] {
  const problems: string[] = []
  const price = used(draft, 'price')
  const estimate = used(draft, 'estimate')
  if (!price && !estimate) problems.push('Enter a purchase price, an estimated customs value, or both.')
  if (price) {
    problems.push(
      ...[
        amountProblem(draft.priceAmount, 'purchase price'),
        draft.priceKind ? null : 'Say whether the purchase price is actual (paid), a quote or an estimate.',
        textProblem(draft.priceSource, 'source of the purchase price', true, SOURCE_MIN, SOURCE_MAX),
        textProblem(draft.priceDocument, 'document reference of the purchase price', false, 1, DOCUMENT_MAX),
      ].filter((problem): problem is string => problem !== null),
    )
  }
  if (estimate) {
    problems.push(
      ...[
        amountProblem(draft.estimateAmount, 'estimated customs value'),
        textProblem(draft.estimateSource, 'source of the estimated customs value', true, SOURCE_MIN, SOURCE_MAX),
        textProblem(draft.estimateDocument, 'document reference of the estimate', false, 1, DOCUMENT_MAX),
      ].filter((problem): problem is string => problem !== null),
    )
  }
  return problems
}

/** The request body of a draft: only the values given, trimmed; no document reference when empty. */
export function requestOf(draft: Draft): Omit<ImportValuesRecordRequest, 'idempotency_key'> {
  const body: Omit<ImportValuesRecordRequest, 'idempotency_key'> = {}
  if (used(draft, 'price') && draft.priceKind) {
    body.purchase_price = {
      amount: draft.priceAmount.trim(),
      currency: 'EUR',
      kind: draft.priceKind,
      source: draft.priceSource.trim(),
      ...(draft.priceDocument.trim() ? { document_ref: draft.priceDocument.trim() } : {}),
    }
  }
  if (used(draft, 'estimate')) {
    body.estimated_customs_value = {
      amount_eur: draft.estimateAmount.trim(),
      source: draft.estimateSource.trim(),
      ...(draft.estimateDocument.trim() ? { document_ref: draft.estimateDocument.trim() } : {}),
    }
  }
  return body
}

function RecordForm({ listingId, onRecorded }: { listingId: string; onRecorded: () => void }) {
  const { client } = useWorkspace()
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [confirmed, setConfirmed] = useState(false)
  const [tried, setTried] = useState(false)
  const mutation = useIdempotentMutation<ImportValuesRecordRequest, ImportValuesRecordResult>(
    'import-values',
    (body) => client.recordImportValues(listingId, body),
    {
      settled: (_attempt, outcome) => {
        if ('envelope' in outcome) {
          setDraft(EMPTY)
          setConfirmed(false)
          setTried(false)
          onRecorded()
        }
      },
    },
  )
  const locked = mutation.phase.kind === 'pending' || mutation.phase.kind === 'unconfirmed'
  const problems = draftProblems(draft)
  const serverFields = new Set(
    mutation.phase.kind === 'rejected' && mutation.phase.error.code === 'VALIDATION_ERROR' ? mutation.phase.error.fields : [],
  )
  const invalid = (key: keyof Draft): true | undefined => {
    const field = FIELD_OF[key]
    const group = field.split('.')[0] ?? ''
    return serverFields.has(field) || serverFields.has(group) || undefined
  }
  const set = (key: keyof Draft, value: string) => setDraft((previous) => ({ ...previous, [key]: value }))
  const input = (key: keyof Draft, labelText: ReactNode, options: { inputMode?: 'decimal' | 'text'; maxLength?: number; help?: string } = {}) => {
    const id = `import-values-${key}`
    return (
      <div className="field">
        <label htmlFor={id}>{labelText}</label>
        <input
          id={id}
          value={draft[key]}
          inputMode={options.inputMode ?? 'text'}
          autoComplete="off"
          maxLength={options.maxLength}
          aria-invalid={invalid(key)}
          disabled={locked}
          onChange={(event) => set(key, event.target.value)}
        />
        {options.help ? <span className="field-hint">{options.help}</span> : null}
      </div>
    )
  }

  return (
    <form
      className="form import-values-form"
      aria-label="Record import values"
      data-testid="import-values-form"
      onSubmit={(event) => {
        event.preventDefault()
        if (locked || !confirmed) return
        if (problems.length) {
          setTried(true)
          return
        }
        void mutation.submit(requestOf(draft))
      }}
    >
      <p className="panel-title">Record or update (owner only)</p>
      <fieldset>
        <legend>
          {PURCHASE_TITLE} · <span lang="mk">{PURCHASE_TITLE_MK}</span>
        </legend>
        <div className="calc-grid">
          {input('priceAmount', 'Purchase price (EUR)', { inputMode: 'decimal', maxLength: 16, help: 'What is, or will be, actually paid, e.g. 2800.00' })}
          <div className="field">
            <label htmlFor="import-values-priceKind">Kind</label>
            <select
              id="import-values-priceKind"
              value={draft.priceKind}
              aria-invalid={invalid('priceKind')}
              disabled={locked}
              onChange={(event) => set('priceKind', event.target.value)}
            >
              <option value="">Choose</option>
              <option value="actual">Actual (paid: invoice or contract)</option>
              <option value="quote">Quote (offered or agreed, not paid yet)</option>
              <option value="estimate">Estimate</option>
            </select>
          </div>
          {input('priceSource', 'Source of the purchase price', { maxLength: SOURCE_MAX, help: 'e.g. "seller invoice"' })}
          {input('priceDocument', 'Document reference (optional)', { maxLength: DOCUMENT_MAX })}
        </div>
      </fieldset>
      <fieldset>
        <legend>
          {CUSTOMS_TITLE} · <span lang="mk">{CUSTOMS_TITLE_MK}</span>
        </legend>
        <div className="calc-grid">
          {input('estimateAmount', 'Estimated customs value (EUR, without transport)', {
            inputMode: 'decimal',
            maxLength: 16,
            help: 'The vehicle only, e.g. the SchwackeNet catalogue value 4200',
          })}
          {input('estimateSource', 'Source of the estimated customs value', { maxLength: SOURCE_MAX, help: 'e.g. "SchwackeNet catalogue"' })}
          {input('estimateDocument', 'Document reference of the estimate (optional)', { maxLength: DOCUMENT_MAX })}
        </div>
      </fieldset>
      <p className="muted small">Leave a group empty to keep its current value. Amounts in EUR with a dot (e.g. 2800.00).</p>
      {tried && problems.length ? (
        <ul className="form-error" data-testid="import-values-problems">
          {problems.map((problem) => (
            <li key={problem}>{problem}</li>
          ))}
        </ul>
      ) : null}
      <Notice tone="warn">
        <span data-testid="import-values-effect">{RECORD_EFFECT}</span>
      </Notice>
      <label className="checkbox">
        <input type="checkbox" checked={confirmed} disabled={locked} onChange={(event) => setConfirmed(event.target.checked)} /> I
        understand that this changes this listing&apos;s valuation and confirm the values above.
      </label>
      <div className="form-actions">
        <button type="submit" className="button" disabled={locked || !confirmed}>
          Record values
        </button>
        <button
          type="button"
          className="button secondary"
          disabled={locked}
          onClick={() => {
            setDraft(EMPTY)
            setTried(false)
          }}
        >
          Clear
        </button>
      </div>
      <MutationStatus
        phase={mutation.phase}
        subject="import values"
        onRetry={() => void mutation.retry()}
        onDiscard={() => {
          mutation.reset()
          onRecorded()
        }}
        confirmed={(result) => (
          <span data-testid="import-values-recorded">
            Recorded {result.recorded_ids.length === 1 ? 'one value' : `${result.recorded_ids.length} values`}
            {result.superseded_ids.length ? ` (replacing ${result.superseded_ids.length} earlier)` : ''}.{' '}
            {result.stale_valuation_ids.length
              ? `${result.stale_valuation_ids.length} valuation(s) of this listing marked stale; the recomputation is queued.`
              : "The recomputation of this listing's valuation is queued."}{' '}
            <span className="muted">{result.production_effect}</span>
          </span>
        )}
      />
    </form>
  )
}
