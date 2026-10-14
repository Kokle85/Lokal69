/**
 * The candidate's "Import values" panel (owner decision 2026-10-13: the real purchase price and the
 * estimated customs value are two separate values per vehicle; `GET` / `POST
 * /api/listings/{listing_id}/import-values`). SYNTHETIC data only.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { draftProblems, requestOf } from '../screens/importValues/ImportValuesPanel'
import { apiError, fakeApi, ok, type Handler } from './fakeApi'
import { candidateDetail, incompleteValuation, LISTING_ID, me } from './fixtures'
import { importValuesView, noImportValues, recordResult } from './importValuesFixtures'
import { renderApp } from './renderApp'
import type { Role } from '../api/types'

const GET = 'GET /api/listings/:id/import-values'
const POST = 'POST /api/listings/:id/import-values'

function valuesApi(role: Role, routes: Record<string, Handler> = {}) {
  return fakeApi({
    'GET /api/me': () => ok(me(role)),
    'GET /api/candidates/:id': () => ok(candidateDetail()),
    [GET]: () => ok(importValuesView()),
    ...routes,
  })
}

async function openPanel(role: Role, routes: Record<string, Handler> = {}) {
  const api = valuesApi(role, routes)
  renderApp(`/candidates/${LISTING_ID}`, { api })
  const panel = await screen.findByRole('region', { name: 'Import values' })
  await within(panel).findByTestId('import-values-current')
  return { api, panel }
}

const EMPTY_DRAFT = {
  priceAmount: '',
  priceKind: '' as const,
  priceSource: '',
  priceDocument: '',
  estimateAmount: '',
  estimateSource: '',
  estimateDocument: '',
}

describe('import values panel', () => {
  it('shows the current recorded values side by side in whole euros, with source and history, to everyone', async () => {
    const { api, panel } = await openPanel('viewer')
    expect(api.callsTo(GET)[0]?.path).toBe(`/api/listings/${LISTING_ID}/import-values`)
    const price = within(panel).getByTestId('recorded-purchase-price')
    expect(price).toHaveTextContent('Purchase price (real, for profit)')
    expect(within(price).getByText('Реална набавна цена (за профит)')).toHaveAttribute('lang', 'mk')
    expect(price).toHaveTextContent('EUR 2,801') // the server's whole euros (2800.50 was recorded)
    expect(price).not.toHaveTextContent('2800.50')
    expect(price).toHaveTextContent('Kind: actual (paid)')
    expect(price).toHaveTextContent('Source: seller invoice · document INV-SYNTHETIC-1')
    const customs = within(panel).getByTestId('recorded-customs-value')
    expect(customs).toHaveTextContent('Estimated customs value (for duties)')
    expect(within(customs).getByText('Проценета царинска вредност (за давачки)')).toHaveAttribute('lang', 'mk')
    expect(customs).toHaveTextContent('EUR 4,200')
    expect(customs).toHaveTextContent('Source: SchwackeNet catalogue')
    expect(customs).toHaveTextContent('without transport')
    // A short history: the superseded quote is kept and labelled.
    const history = within(panel).getByRole('table', { name: 'Import value history' })
    const rows = within(history).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(3)
    expect(rows[2]).toHaveTextContent('EUR 2,750')
    expect(rows[2]).toHaveTextContent('quote')
    expect(rows[2]).toHaveTextContent('superseded')
    expect(rows[1]).toHaveTextContent('current')
    for (const amount of panel.querySelectorAll('.amount')) expect(amount.textContent).not.toMatch(/\d\.\d/)
    // The calculator prefilled from the listing (it uses these recorded values).
    expect(within(panel).getByTestId('import-values-calculator-link')).toHaveAttribute(
      'href',
      `/candidates/${LISTING_ID}/import-costs`,
    )
    // Viewers and reviewers read; only the owner records.
    expect(within(panel).queryByTestId('import-values-form')).toBeNull()
    expect(within(panel).getByTestId('import-values-owner-only')).toHaveTextContent('Only the workspace owner')
  })

  it('says "not recorded" (never 0) when nothing is recorded', async () => {
    const { panel } = await openPanel('reviewer', { [GET]: () => ok(noImportValues()) })
    expect(within(panel).getByTestId('recorded-purchase-price-none')).toHaveTextContent(
      'Not recorded. Valuations and the calculator use the advertised price.',
    )
    expect(within(panel).getByTestId('recorded-customs-value-none')).toHaveTextContent(
      'Not recorded. The import charges use the price paid (transaction value).',
    )
    expect(panel).not.toHaveTextContent(/EUR 0\b/)
    expect(within(panel).getByText('Nothing recorded for this listing yet.')).toBeInTheDocument()
    expect(within(panel).queryByTestId('import-values-form')).toBeNull()
  })

  it('shows a load failure with a retry instead of values', async () => {
    let calls = 0
    const api = valuesApi('viewer', {
      [GET]: () => {
        calls += 1
        return calls === 1 ? apiError(503, 'DEPENDENCY_UNAVAILABLE', 'database unavailable', { retryable: true }) : ok(noImportValues())
      },
    })
    renderApp(`/candidates/${LISTING_ID}`, { api })
    const panel = await screen.findByRole('region', { name: 'Import values' })
    await user().click(await within(panel).findByRole('button', { name: 'Retry' }))
    expect(await within(panel).findByTestId('recorded-purchase-price-none')).toBeInTheDocument()
  })

  it('lets the owner record both values after confirming the effect on the valuation', async () => {
    let values = noImportValues()
    const { api, panel } = await openPanel('owner', {
      [GET]: () => ok(values),
      [POST]: () => {
        values = importValuesView()
        return ok(recordResult(values))
      },
    })
    const u = user()
    const form = within(panel).getByTestId('import-values-form')
    expect(within(form).getByTestId('import-values-effect')).toHaveTextContent(
      "Recording changes this listing's valuation: its current valuation is marked stale and recomputed",
    )
    await u.type(within(form).getByLabelText('Purchase price (EUR)'), ' 2800.50 ')
    await u.selectOptions(within(form).getByLabelText('Kind'), 'actual')
    await u.type(within(form).getByLabelText('Source of the purchase price'), 'seller invoice')
    await u.type(within(form).getByLabelText('Estimated customs value (EUR, without transport)'), '4200')
    await u.type(within(form).getByLabelText('Source of the estimated customs value'), ' SchwackeNet catalogue ')
    const submit = within(form).getByRole('button', { name: 'Record values' })
    expect(submit).toBeDisabled() // not before the owner confirms the effect
    await u.click(within(form).getByLabelText(/I understand that this changes this listing's valuation/))
    await u.click(submit)

    expect(await within(panel).findByTestId('import-values-recorded')).toHaveTextContent(
      'Recorded 2 values (replacing 1 earlier). 1 valuation(s) of this listing marked stale; the recomputation is queued.',
    )
    const [sent] = api.callsTo(POST)
    expect(api.callsTo(POST)).toHaveLength(1)
    expect(sent?.path).toBe(`/api/listings/${LISTING_ID}/import-values`)
    const body = sent?.body as Record<string, unknown>
    expect(body).toEqual({
      purchase_price: { amount: '2800.50', currency: 'EUR', kind: 'actual', source: 'seller invoice' },
      estimated_customs_value: { amount_eur: '4200', source: 'SchwackeNet catalogue' },
      idempotency_key: expect.stringMatching(/^import-values:/),
    })
    expect(sent?.headers.get('Idempotency-Key')).toBe(body.idempotency_key)
    // The values and the candidate (its valuation state) are reloaded; the form is cleared.
    await waitFor(() => expect(within(panel).getByTestId('recorded-purchase-price')).toHaveTextContent('EUR 2,801'))
    expect(api.callsTo(GET).length).toBeGreaterThanOrEqual(2)
    expect(api.callsTo('GET /api/candidates/:id').length).toBeGreaterThanOrEqual(2)
    expect(within(form).getByLabelText('Purchase price (EUR)')).toHaveValue('')
    expect(within(form).getByLabelText(/I understand/)).not.toBeChecked()
  })

  it('records one value alone, with its document reference', async () => {
    const { api, panel } = await openPanel('owner', { [POST]: () => ok(recordResult()) })
    const u = user()
    const form = within(panel).getByTestId('import-values-form')
    await u.type(within(form).getByLabelText('Estimated customs value (EUR, without transport)'), '4200')
    await u.type(within(form).getByLabelText('Source of the estimated customs value'), 'SchwackeNet catalogue')
    await u.type(within(form).getByLabelText('Document reference of the estimate (optional)'), 'SN-2026-10')
    await u.click(within(form).getByLabelText(/I understand/))
    await u.click(within(form).getByRole('button', { name: 'Record values' }))
    await within(panel).findByTestId('import-values-recorded')
    expect(api.callsTo(POST)[0]?.body).toEqual({
      estimated_customs_value: { amount_eur: '4200', source: 'SchwackeNet catalogue', document_ref: 'SN-2026-10' },
      idempotency_key: expect.any(String),
    })
  })

  it('checks the draft before sending and sends nothing while it has problems', async () => {
    const { api, panel } = await openPanel('owner', { [POST]: () => ok(recordResult()) })
    const u = user()
    const form = within(panel).getByTestId('import-values-form')
    await u.click(within(form).getByLabelText(/I understand/))
    await u.click(within(form).getByRole('button', { name: 'Record values' }))
    expect(within(form).getByTestId('import-values-problems')).toHaveTextContent(
      'Enter a purchase price, an estimated customs value, or both.',
    )
    await u.type(within(form).getByLabelText('Purchase price (EUR)'), '0.00')
    await u.click(within(form).getByRole('button', { name: 'Record values' }))
    const problems = within(form).getByTestId('import-values-problems')
    expect(problems).toHaveTextContent('The purchase price is more than zero')
    expect(problems).toHaveTextContent('Say whether the purchase price is actual (paid), a quote or an estimate.')
    expect(problems).toHaveTextContent('Name the source of the purchase price.')
    expect(api.callsTo(POST)).toHaveLength(0)
  })

  it('marks the fields a server-side validation error names (values are never echoed)', async () => {
    const { api, panel } = await openPanel('owner', {
      [POST]: () =>
        apiError(422, 'VALIDATION_ERROR', 'Invalid purchase price', { details: { fields: ['purchase_price.amount'] } }),
    })
    const u = user()
    const form = within(panel).getByTestId('import-values-form')
    await u.type(within(form).getByLabelText('Purchase price (EUR)'), '9999999999999')
    await u.selectOptions(within(form).getByLabelText('Kind'), 'quote')
    await u.type(within(form).getByLabelText('Source of the purchase price'), 'agreed with the seller')
    await u.click(within(form).getByLabelText(/I understand/))
    await u.click(within(form).getByRole('button', { name: 'Record values' }))
    expect(await within(panel).findByTestId('mutation-rejected')).toHaveTextContent('VALIDATION_ERROR')
    expect(within(form).getByLabelText('Purchase price (EUR)')).toHaveAttribute('aria-invalid', 'true')
    expect(within(form).getByLabelText('Source of the purchase price')).not.toHaveAttribute('aria-invalid')
    expect(api.callsTo(POST)).toHaveLength(1)
  })

  it('retries a lost answer with the same idempotency key and body', async () => {
    let attempts = 0
    const { api, panel } = await openPanel('owner', {
      [POST]: () => {
        attempts += 1
        if (attempts === 1) throw new TypeError('network connection lost') // committed; the answer never arrived
        return ok(recordResult())
      },
    })
    const u = user()
    const form = within(panel).getByTestId('import-values-form')
    await u.type(within(form).getByLabelText('Purchase price (EUR)'), '2800')
    await u.selectOptions(within(form).getByLabelText('Kind'), 'actual')
    await u.type(within(form).getByLabelText('Source of the purchase price'), 'seller invoice')
    await u.click(within(form).getByLabelText(/I understand/))
    await u.click(within(form).getByRole('button', { name: 'Record values' }))
    const unconfirmed = await within(panel).findByTestId('mutation-unconfirmed')
    expect(within(form).getByRole('button', { name: 'Record values' })).toBeDisabled()
    expect(within(form).getByLabelText('Purchase price (EUR)')).toBeDisabled()
    await u.click(within(unconfirmed).getByRole('button', { name: 'Retry the same request' }))
    expect(await within(panel).findByTestId('mutation-confirmed')).toHaveTextContent('confirmed by the server on an idempotent retry')
    const [first, second] = api.callsTo(POST)
    expect(second?.body).toEqual(first?.body)
    expect(second?.headers.get('Idempotency-Key')).toBe(first?.headers.get('Idempotency-Key'))
  })

  it('reviewers see the values but no recording form', async () => {
    const { panel } = await openPanel('reviewer')
    expect(within(panel).getByTestId('recorded-purchase-price')).toHaveTextContent('EUR 2,801')
    expect(within(panel).queryByRole('button', { name: 'Record values' })).toBeNull()
  })

  it('is pointed to from the economics screen', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me()),
      'GET /api/candidates/:id': () => ok(candidateDetail()),
      'GET /api/valuations/:id': () => ok(incompleteValuation()),
    })
    renderApp(`/candidates/${LISTING_ID}/economics`, { api })
    const hint = await screen.findByTestId('import-values-hint')
    expect(within(hint).getByRole('link', { name: 'Import values on the candidate' })).toHaveAttribute(
      'href',
      `/candidates/${LISTING_ID}`,
    )
  })
})

describe('import values draft', () => {
  it('builds only the given values, trimmed, EUR only, without an empty document reference', () => {
    expect(requestOf({ ...EMPTY_DRAFT, priceAmount: ' 2800 ', priceKind: 'estimate', priceSource: ' owner ' })).toEqual({
      purchase_price: { amount: '2800', currency: 'EUR', kind: 'estimate', source: 'owner' },
    })
    expect(requestOf({ ...EMPTY_DRAFT, estimateAmount: '4200', estimateSource: 'SchwackeNet catalogue', estimateDocument: ' ' })).toEqual({
      estimated_customs_value: { amount_eur: '4200', source: 'SchwackeNet catalogue' },
    })
  })

  it('names the problems of a draft (amount format, zero, source length, hidden characters)', () => {
    expect(draftProblems(EMPTY_DRAFT)).toEqual(['Enter a purchase price, an estimated customs value, or both.'])
    expect(draftProblems({ ...EMPTY_DRAFT, estimateAmount: '4200,50', estimateSource: 'ab' })).toEqual([
      'The estimated customs value is a plain amount in EUR with a dot and at most two decimals (e.g. 2800.00).',
      'The source of the estimated customs value has 3 to 120 characters.',
    ])
    expect(draftProblems({ ...EMPTY_DRAFT, estimateAmount: '4200.505', estimateSource: 'Schwacke‮Net' })).toEqual([
      'The estimated customs value is a plain amount in EUR with a dot and at most two decimals (e.g. 2800.00).',
      'The source of the estimated customs value contains control or direction-changing characters.',
    ])
    expect(draftProblems({ ...EMPTY_DRAFT, priceAmount: '2800', priceKind: 'actual', priceSource: 'seller invoice' })).toEqual([])
  })
})

function user() {
  return userEvent.setup()
}
