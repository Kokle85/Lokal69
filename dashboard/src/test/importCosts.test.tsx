import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { apiError, fakeApi, ok } from './fakeApi'
import { candidateDetail, LISTING_ID, me } from './fixtures'
import {
  BANNER_EN,
  BANNER_MK,
  importEstimate,
  incompleteScenario,
  listingEstimate,
  ownerExampleEstimate,
  previewScenario,
  RECORDED_PURCHASE_ID,
  recordedListingEstimate,
} from './importCostFixtures'
import { renderApp } from './renderApp'
import { formFromSearch, queryFromSearch, searchFromForm } from '../screens/importCosts/form'

const ROUTE = 'GET /api/import-costs/estimate'
/** The real purchase price field ("Purchase price (real, for profit) · Реална набавна цена (за профит)"). */
const PRICE_LABEL = /^Purchase price \(real, for profit\)/
const ESTIMATE_LABEL = /^Estimated customs value \(for duties\)/
const ESTIMATE_SOURCE_LABEL = /^Source of the estimated customs value/

function calculatorApi(handler = () => ok(importEstimate(), 200, { warnings: [{ code: 'TAX_RULES_UNAPPROVED', message: BANNER_EN }] })) {
  return fakeApi({ 'GET /api/me': () => ok(me('viewer')), [ROUTE]: handler })
}

describe('customs calculator form', () => {
  it('maps the form to the flat query parameters: only given values, checkboxes only when ticked', () => {
    const form = formFromSearch(new URLSearchParams('price_amount=%203000.00%20&fuel=diesel&co2_clear=false&co2_round_up_confirmed=true&junk=1'))
    expect(form.price_amount).toBe(' 3000.00 ')
    expect(form.co2_clear).toBe('')
    expect(searchFromForm(form).toString()).toBe('price_amount=3000.00&fuel=diesel&co2_round_up_confirmed=true')
    expect(queryFromSearch(new URLSearchParams('fuel=petrol&accept_previous_month_fx=true'), LISTING_ID)).toEqual({
      fuel: 'petrol',
      accept_previous_month_fx: true,
      listing_id: LISTING_ID,
    })
  })

  it('calculates from the entered facts and labels the indicative preview in Macedonian and English', async () => {
    const api = calculatorApi()
    const { router } = renderApp('/import-costs', { api })
    const user = userEvent.setup()
    expect(await screen.findByRole('heading', { name: 'Customs calculator', level: 1 })).toBeInTheDocument()
    expect(within(screen.getByRole('navigation', { name: 'Primary' })).getByRole('link', { name: 'Customs calculator' })).toHaveClass(
      'active',
    )
    expect(api.callsTo(ROUTE)).toHaveLength(0) // nothing to calculate yet
    await user.type(screen.getByLabelText(PRICE_LABEL), '3000.00')
    await user.selectOptions(screen.getByLabelText('Price currency'), 'EUR')
    await user.selectOptions(screen.getByLabelText('Price basis'), 'invoice')
    await user.selectOptions(screen.getByLabelText('Fuel / drive'), 'diesel')
    await user.type(screen.getByLabelText('CO2 NEDC (g/km)'), '189')
    await user.selectOptions(screen.getByLabelText('CO2 document'), 'coc')
    await user.type(screen.getByLabelText('Customs rate EUR/MKD'), '61.5337')
    await user.click(screen.getByRole('button', { name: 'Calculate' }))

    await waitFor(() => expect(api.callsTo(ROUTE)).toHaveLength(1))
    const sent = api.callsTo(ROUTE)[0]!.url.searchParams
    expect(Object.fromEntries(sent)).toEqual({
      price_amount: '3000.00',
      price_currency: 'EUR',
      price_basis: 'invoice',
      fuel: 'diesel',
      co2_nedc_g_km: '189',
      co2_document: 'coc',
      customs_eur_mkd_rate: '61.5337',
    })
    expect(sent.has('listing_id')).toBe(false)
    expect(router.state.location.search).toContain('co2_nedc_g_km=189')

    const banner = await screen.findByTestId('estimate-banner')
    expect(banner).toHaveTextContent('INDICATIVE PREVIEW')
    expect(banner).toHaveTextContent('ИНДИКАТИВНА ПРОЦЕНКА')
    expect(within(banner).getByText(BANNER_MK)).toHaveAttribute('lang', 'mk')
    expect(within(banner).getByText(BANNER_EN)).toHaveAttribute('lang', 'en')
    const totals = screen.getByTestId('totals-primary')
    expect(totals).toHaveTextContent('Indicative total import charges (MKD)')
    expect(totals).toHaveTextContent('MKD 168,155')
    expect(totals).toHaveTextContent('EUR 2,733')
    expect(totals).toHaveTextContent('Indicative landed cost (EUR)')
    expect(totals).toHaveTextContent('EUR 6,433')
    expect(totals).not.toHaveTextContent(/^Total import charges/)
    // Helper components are secondary rows, never counted in a total.
    const components = screen.getByRole('table', { name: 'Components (primary)' })
    const helper = within(components).getByText('customs_value_mkd').closest('tr')!
    expect(helper).toHaveClass('row-intermediate')
    expect(within(helper).getByText(/intermediate · not counted in the total/)).toBeInTheDocument()
    const duty = within(components).getByText('duty_standard').closest('tr')!
    expect(duty).not.toHaveClass('row-intermediate')
    expect(within(duty).getByText('MKD 11,384')).toBeInTheDocument()
    const categories = screen.getByRole('table', { name: 'Import charges by category (primary)' })
    expect(within(categories).getByText('motor vehicle tax').closest('tr')).toHaveTextContent('MKD 96,390')
    // The cost profile's transport line, as the server states it (the browser adds nothing to it).
    const basis = screen.getByRole('region', { name: 'Basis' })
    expect(within(basis).getByText('Transport (cost profile)').nextElementSibling).toHaveTextContent('700 EUR per vehicle')
    expect(screen.getByText(/Euro 5 meets the Euro 4 minimum/)).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Uncertain items' }).firstElementChild).toHaveTextContent('applies')
    expect(screen.getByText(/tax rules unapproved/)).toBeInTheDocument() // the envelope warning
  })

  it('shows unknown charges as "unknown", never as 0, with the known subtotal labelled as not a total', async () => {
    const view = importEstimate({ scenarios: [incompleteScenario()], missing_facts: ['co2'] })
    const api = calculatorApi(() => ok(view, 200, { warnings: [{ code: 'UNKNOWN_COSTS', message: 'Some import charges are unknown (never zero); needed: co2' }] }))
    renderApp('/import-costs?fuel=diesel&price_amount=3000.00&price_currency=EUR', { api })
    const totals = await screen.findByTestId('totals-primary')
    const rows = within(totals).getAllByRole('definition')
    expect(rows[0]).toHaveTextContent(/^unknown/)
    expect(rows[1]).toHaveTextContent(/^unknown/)
    expect(rows[2]).toHaveTextContent(/^unknown/)
    expect(totals).toHaveTextContent('Known subtotal (not a total)')
    expect(totals).not.toHaveTextContent(/MKD 0(\.00)?\b/)
    const components = screen.getByRole('table', { name: 'Components (primary)' })
    for (const id of ['dmv_full', 'vat_import']) {
      const row = within(components).getByText(id).closest('tr')!
      expect(within(row).getByText('unknown', { selector: '.amount' })).toBeInTheDocument()
      expect(row.textContent).not.toMatch(/0\.00/)
    }
    expect(screen.getByText('Needed to complete the calculation:')).toBeInTheDocument()
    expect(screen.getByText(/Unknown charges are\s+never counted as zero/)).toBeInTheDocument()
  })

  it('names what to supply in words and never shows a stale customs rate as the rate used', async () => {
    // Regression: a CHF price was asked for as a missing "price", and a previous month's recorded
    // rate read as "recorded Customs monthly list rate" while no EUR equivalent was computed.
    const view = importEstimate({
      scenarios: [incompleteScenario()],
      missing_facts: ['price_in_eur_or_mkd', 'customs_fx_rate'],
      customs_fx_source: 'recorded_not_effective',
      customs_fx_rate: null,
    })
    const api = calculatorApi(() => ok(view))
    renderApp('/import-costs?fuel=diesel&price_amount=3000.00&price_currency=CHF', { api })
    await screen.findByTestId('totals-primary')
    const needed = screen.getByText('Needed to complete the calculation:').closest('p')!
    expect(needed).toHaveTextContent('Price in EUR or MKD (a CHF price is not converted)')
    expect(needed).toHaveTextContent('Customs rate of the declaration month')
    expect(needed).not.toHaveTextContent(/Purchase price/)
    expect(screen.getByText(/the newest recorded Customs rate is of an earlier month and was not used/)).toBeInTheDocument()
    expect(screen.queryByText(/· recorded Customs monthly list rate/)).not.toBeInTheDocument()
  })

  it('names the fields a validation error points at and never claims a result', async () => {
    const api = calculatorApi(() =>
      apiError(422, 'VALIDATION_ERROR', 'Invalid import-cost estimate request', { details: { fields: ['price_currency'] } }),
    )
    renderApp('/import-costs?price_amount=3000', { api })
    expect(await screen.findByText('Check: Price currency')).toBeInTheDocument()
    expect(screen.getAllByRole('alert').some((node) => node.textContent?.includes('VALIDATION_ERROR'))).toBe(true)
    expect(screen.getByLabelText('Price currency')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText(PRICE_LABEL)).not.toHaveAttribute('aria-invalid')
    expect(screen.getByLabelText(PRICE_LABEL)).toHaveValue('3000')
    expect(screen.queryByTestId('estimate')).toBeNull()
  })

  it('offers a retry after a server failure', async () => {
    let calls = 0
    const api = calculatorApi(() => {
      calls += 1
      return calls === 1 ? apiError(503, 'DEPENDENCY_UNAVAILABLE', 'database unavailable', { retryable: true }) : ok(importEstimate())
    })
    renderApp('/import-costs?fuel=diesel', { api })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Retry' }))
    expect(await screen.findByTestId('estimate-banner')).toBeInTheDocument()
    expect(api.callsTo(ROUTE)).toHaveLength(2)
  })
})

describe('customs calculator from a listing', () => {
  it('is linked from the candidate detail', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()), 'GET /api/candidates/:id': () => ok(candidateDetail()) })
    renderApp(`/candidates/${LISTING_ID}`, { api })
    const related = await screen.findByRole('navigation', { name: 'Related views' })
    expect(within(related).getByRole('link', { name: 'Customs calculator' })).toHaveAttribute(
      'href',
      `/candidates/${LISTING_ID}/import-costs`,
    )
  })

  it("calculates from the listing at once, shows the listing's facts and sends only the overrides", async () => {
    const api = calculatorApi(() => ok(listingEstimate()))
    const { router } = renderApp(`/candidates/${LISTING_ID}/import-costs`, { api })
    const user = userEvent.setup()
    await screen.findByTestId('estimate-banner')
    expect(Object.fromEntries(api.callsTo(ROUTE)[0]!.url.searchParams)).toEqual({ listing_id: LISTING_ID })
    // The empty fields show what the listing established (and keep it when left empty).
    expect(screen.getByText('From the listing: EUR 3,000')).toBeInTheDocument() // the server's whole euros
    expect(screen.getByText('From the listing: 189 g/km (nedc)')).toBeInTheDocument()
    expect(screen.getByLabelText(PRICE_LABEL)).toHaveAttribute('placeholder', 'EUR 3,000')
    expect(screen.getByRole('option', { name: "Keep the listing's (listing claim unverified)" })).toBeInTheDocument()
    expect(screen.getByRole('table', { name: 'Facts used' })).toHaveTextContent('listing')
    expect(screen.getByText(/advertised price, not an invoice/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'synthetic_source' })).toHaveAttribute('href', `/candidates/${LISTING_ID}`)

    api.on(ROUTE, () => ok(listingEstimate()))
    await user.selectOptions(screen.getByLabelText('CO2 document'), 'coc')
    await user.selectOptions(screen.getByLabelText('Proof of origin'), 'eu')
    await user.click(screen.getByRole('button', { name: 'Calculate' }))
    await waitFor(() => expect(api.callsTo(ROUTE)).toHaveLength(2))
    expect(Object.fromEntries(api.callsTo(ROUTE)[1]!.url.searchParams)).toEqual({
      co2_document: 'coc',
      origin_proof_kind: 'eu',
      listing_id: LISTING_ID,
    })
    expect(router.state.location.pathname).toBe(`/candidates/${LISTING_ID}/import-costs`)
    expect(router.state.location.search).toBe('?co2_document=coc&origin_proof_kind=eu')
  })

  it("never promises to keep the listing's CO2 document for a CO2 figure the user enters", async () => {
    // Regression: the select said "Keep the listing's (listing claim unverified)", yet the server
    // drops the listing's document whenever the request sets CO2 figures without one.
    const api = calculatorApi(() => ok(listingEstimate()))
    renderApp(`/candidates/${LISTING_ID}/import-costs`, { api })
    const user = userEvent.setup()
    await screen.findByTestId('estimate-banner')
    const keep = "Keep the listing's (listing claim unverified)"
    expect(screen.getByRole('option', { name: keep })).toBeInTheDocument()
    await user.type(screen.getByLabelText('CO2 WLTP (g/km)'), '210')
    expect(screen.queryByRole('option', { name: keep })).not.toBeInTheDocument()
    expect(
      screen.getByRole('option', { name: 'Not given (a CO2 figure you enter needs its own document)' }),
    ).toBeInTheDocument()
    expect(screen.getByText("The listing's CO2 document is not applied to a CO2 figure you enter.")).toBeInTheDocument()
    await user.clear(screen.getByLabelText('CO2 WLTP (g/km)'))
    expect(screen.getByRole('option', { name: keep })).toBeInTheDocument()
    await user.click(screen.getByLabelText('Ignore the listing CO2 figure'))
    expect(screen.queryByRole('option', { name: keep })).not.toBeInTheDocument()
  })

  it('labels ACTIVE rules as such (totals, no preview banner) and an unavailable result as unknown', async () => {
    const active = importEstimate({
      mode: 'active',
      production_ready: true,
      banner: null,
      rule: { ...importEstimate().rule!, status: 'active', sha256_recorded: true },
      scenarios: [
        previewScenario({
          production_ready: true,
          total: previewScenario().indicative_total,
          total_eur: previewScenario().indicative_total_eur,
          landed_cost_eur: previewScenario().indicative_landed_cost_eur,
          indicative_total: null,
          indicative_total_eur: null,
          indicative_landed_cost_eur: null,
        }),
      ],
    })
    const api = calculatorApi(() => ok(active))
    renderApp(`/candidates/${LISTING_ID}/import-costs`, { api })
    const totals = await screen.findByTestId('totals-primary')
    expect(totals).toHaveTextContent('Total import charges (MKD)')
    expect(totals).toHaveTextContent('MKD 168,155')
    expect(totals).not.toHaveTextContent('Indicative')
    expect(screen.queryByTestId('estimate-banner')).toBeNull()
    expect(screen.getByText(/Owner-approved ACTIVE rules/)).toBeInTheDocument()

    api.on(ROUTE, () =>
      ok(
        importEstimate({
          mode: 'unavailable',
          rule: null,
          scenarios: [],
          banner: { mk: 'Нема применливи правила.', en: 'No applicable rules: every import charge is unknown.' },
          selection_reason: 'no ACTIVE rule set; no draft rule set applies',
          missing_facts: ['tax_rule_set'],
        }),
      ),
    )
    const user = userEvent.setup()
    await user.selectOptions(screen.getByLabelText('Fuel / drive'), 'petrol')
    await user.click(screen.getByRole('button', { name: 'Calculate' }))
    const banner = await screen.findByTestId('estimate-banner')
    expect(banner).toHaveTextContent('UNAVAILABLE')
    expect(banner).toHaveTextContent('every import charge is unknown')
    expect(screen.queryByTestId('totals-primary')).toBeNull()
    expect(screen.getByText('tax rule set')).toBeInTheDocument()
  })
})

describe('two values per vehicle (owner decision 2026-10-13)', () => {
  it('sends the real purchase price and the estimated customs value apart and shows them side by side, in whole units', async () => {
    const api = calculatorApi(() => ok(ownerExampleEstimate()))
    renderApp('/import-costs', { api })
    const user = userEvent.setup()
    const group = await screen.findByTestId('calc-two-values')
    expect(within(group).getByText('Две вредности по возило')).toHaveAttribute('lang', 'mk')
    // English and Macedonian labels of the two values.
    expect(screen.getByLabelText(PRICE_LABEL)).toHaveAccessibleName(
      'Purchase price (real, for profit) · Реална набавна цена (за профит)',
    )
    expect(screen.getByLabelText(ESTIMATE_LABEL)).toHaveAccessibleName(
      'Estimated customs value (for duties) · Проценета царинска вредност (за давачки)',
    )
    expect(within(group).getByText('Реална набавна цена (за профит)')).toHaveAttribute('lang', 'mk')
    await user.type(screen.getByLabelText(PRICE_LABEL), '2800')
    await user.selectOptions(screen.getByLabelText('Price currency'), 'EUR')
    await user.selectOptions(screen.getByLabelText('Price basis'), 'invoice')
    await user.type(screen.getByLabelText(ESTIMATE_LABEL), '4200')
    await user.type(screen.getByLabelText(ESTIMATE_SOURCE_LABEL), 'SchwackeNet catalogue')
    await user.click(screen.getByRole('button', { name: 'Calculate' }))
    await waitFor(() => expect(api.callsTo(ROUTE)).toHaveLength(1))
    expect(Object.fromEntries(api.callsTo(ROUTE)[0]!.url.searchParams)).toEqual({
      price_amount: '2800',
      price_currency: 'EUR',
      price_basis: 'invoice',
      estimated_customs_value_eur: '4200',
      estimated_customs_value_source: 'SchwackeNet catalogue',
    })

    // The two values side by side, each with its basis and source.
    const price = await screen.findByTestId('value-purchase-price')
    expect(price).toHaveTextContent('Purchase price (real, for profit)')
    expect(price).toHaveTextContent('EUR 2,800')
    expect(price).toHaveTextContent('Basis: invoice / contract price (actually paid)')
    expect(price).toHaveTextContent('Every landed cost below uses this price')
    const customs = screen.getByTestId('value-customs-value')
    expect(customs).toHaveTextContent('EUR 4,900')
    expect(customs).toHaveTextContent('Basis: estimated customs value + transport to the MK border')
    expect(customs).toHaveTextContent('Source: SchwackeNet catalogue')
    expect(within(customs).getByText('Царинска вредност за давачките')).toHaveAttribute('lang', 'mk')

    // The primary scenario and the transaction-value alternative side by side (server figures only).
    const glance = screen.getByRole('table', { name: 'Scenarios side by side' })
    const primary = within(glance).getByTestId('glance-primary')
    expect(primary).toHaveTextContent('EUR 4,900')
    expect(primary).toHaveTextContent('MKD 185,803')
    expect(primary).toHaveTextContent('EUR 3,020')
    expect(primary).toHaveTextContent('EUR 6,520')
    const alternative = within(glance).getByTestId('glance-transaction_value')
    expect(alternative).toHaveTextContent('Alternative: Customs accepts the price paid (transaction value)')
    expect(alternative).toHaveTextContent('EUR 3,500')
    expect(alternative).toHaveTextContent('MKD 165,213')
    expect(alternative).toHaveTextContent('EUR 6,185')
    expect(screen.getByTestId('totals-transaction_value')).toHaveTextContent('EUR 6,185')
    // The banner stays, in Macedonian and English.
    expect(screen.getByTestId('estimate-banner')).toHaveTextContent('ИНДИКАТИВНА ПРОЦЕНКА · INDICATIVE PREVIEW')

    // No amount anywhere in the result carries decimals (the server sends whole units; the browser never rounds).
    const amounts = [...screen.getByTestId('estimate').querySelectorAll('.amount')].map((node) => node.textContent ?? '')
    expect(amounts.length).toBeGreaterThan(20)
    for (const amount of amounts) expect(amount).not.toMatch(/\d\.\d/)
    // The facts used echo the estimate without a zero fraction and the price as the server's whole euros.
    const facts = screen.getByRole('table', { name: 'Facts used' })
    expect(within(facts).getByText('Estimated customs value (EUR)').closest('tr')).toHaveTextContent('EUR 4,200')
    expect(within(facts).getByText('Purchase price (real)').closest('tr')).toHaveTextContent('EUR 2,800')
  })

  it('without an estimate shows the transaction value as the customs value and no alternative', async () => {
    const api = calculatorApi(() => ok(importEstimate()))
    renderApp('/import-costs?price_amount=3000.00&price_currency=EUR&price_basis=invoice', { api })
    const customs = await screen.findByTestId('value-customs-value')
    expect(customs).toHaveTextContent('EUR 3,700')
    expect(customs).toHaveTextContent('Basis: transaction value: the price paid + transport to the MK border (Customs Law art. 28)')
    expect(customs).toHaveTextContent('No estimated customs value given')
    expect(screen.queryByTestId('glance-transaction_value')).toBeNull()
    expect(screen.getByTestId('glance-primary')).toHaveTextContent('EUR 6,433')
  })

  it('compares the scenario customs values in EUR even when a transaction value is in MKD', async () => {
    // Regression: the "Customs value (EUR)" column rendered each scenario's customs value in its own currency,
    // so an MKD price put "MKD 215,368" under the EUR header next to the primary's EUR 4,900.
    const view = ownerExampleEstimate()
    const [primary, alternative] = view.scenarios
    const mkdAlternative = {
      ...alternative!,
      customs_value: { status: 'known' as const, amount: '215368', currency: 'MKD', reason: null },
      customs_value_eur: { status: 'known' as const, amount: '3500', currency: 'EUR', reason: null },
    }
    const api = calculatorApi(() => ok({ ...view, scenarios: [primary!, mkdAlternative] }))
    renderApp('/import-costs?price_amount=172294&price_currency=MKD', { api })
    const glance = await screen.findByRole('table', { name: 'Scenarios side by side' })
    const cells = (name: string) =>
      within(within(glance).getByTestId(`glance-${name}`))
        .getAllByRole('cell')
        .map((cell) => [cell.getAttribute('data-label'), cell.textContent])
    expect(cells('primary')).toContainEqual(['Customs value (EUR)', 'EUR 4,900'])
    expect(cells('transaction_value')).toContainEqual(['Customs value (EUR)', 'EUR 3,500'])
    expect(within(glance).getByTestId('glance-transaction_value')).not.toHaveTextContent('MKD 215,368')
    // The scenario's own section keeps the engine's MKD customs value, with the EUR equivalent next to it.
    expect(screen.getByTestId('totals-transaction_value')).toHaveTextContent('MKD 215,368 (EUR 3,500)')
    expect(screen.getByTestId('totals-primary')).not.toHaveTextContent('(EUR 4,900)')
  })

  it('shows an unknown purchase price as unknown, never 0', async () => {
    const unknownPrice = {
      status: 'unknown' as const,
      amount: null,
      currency: 'EUR',
      reason: 'purchase price unknown',
    }
    const view = importEstimate({
      purchase_price: { amount: unknownPrice, basis: null, source: 'no purchase price given' },
      customs_value_for_duties: { amount: { ...unknownPrice, reason: 'no price' }, basis: 'transaction_value', source: 'no purchase price given' },
      facts: { ...importEstimate().facts, price: unknownPrice },
      scenarios: [incompleteScenario()],
      missing_facts: ['price'],
    })
    const api = calculatorApi(() => ok(view))
    renderApp('/import-costs?fuel=diesel', { api })
    const price = await screen.findByTestId('value-purchase-price')
    expect(price).toHaveTextContent('unknown (purchase price unknown)')
    expect(price).toHaveTextContent('Basis: not given')
    expect(price).not.toHaveTextContent(/EUR 0\b/)
    expect(screen.getByTestId('value-customs-value')).toHaveTextContent('unknown')
  })

  it("on the listing route keeps the owner's recorded values, labelled as recorded", async () => {
    const api = calculatorApi(() => ok(recordedListingEstimate()))
    renderApp(`/candidates/${LISTING_ID}/import-costs`, { api })
    const user = userEvent.setup()
    await screen.findByTestId('estimate-banner')
    expect(Object.fromEntries(api.callsTo(ROUTE)[0]!.url.searchParams)).toEqual({ listing_id: LISTING_ID })
    // The recorded price shows as the server's whole euros (2800.50 was recorded; the browser never rounds).
    expect(screen.getByText('Recorded for this listing: EUR 2,801')).toHaveAttribute('data-hint-origin', 'recorded')
    expect(screen.getByLabelText(PRICE_LABEL)).toHaveAttribute('placeholder', 'EUR 2,801')
    expect(screen.getByText('Recorded for this listing: EUR 4,200')).toBeInTheDocument()
    expect(screen.getByText('Recorded for this listing: SchwackeNet catalogue')).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Keep the recorded (recorded: actual (paid))' })).toBeInTheDocument()
    expect(screen.getByText('From the listing: 189 g/km (nedc)')).toBeInTheDocument()
    const price = screen.getByTestId('value-purchase-price')
    expect(price).toHaveTextContent('EUR 2,801')
    expect(price).toHaveTextContent('Basis: recorded by the owner: actual (paid)')
    const facts = screen.getByRole('table', { name: 'Facts used' })
    const priceRow = within(facts).getByText('Purchase price (real)').closest('tr')!
    expect(priceRow).toHaveTextContent('EUR 2,801')
    expect(priceRow).toHaveTextContent('recorded value')
    expect(within(facts).getByText('Estimated customs value (EUR)').closest('tr')).toHaveTextContent('EUR 4,200recorded value')
    expect(screen.getByTestId('recorded-values-note')).toHaveTextContent("The owner's recorded import values of this listing apply")
    expect(within(screen.getByTestId('recorded-values-note')).getByRole('link')).toHaveAttribute('href', `/candidates/${LISTING_ID}`)
    expect(screen.getByRole('list', { name: 'Estimate warnings' })).toHaveTextContent(`RECORDED_PURCHASE_PRICE_USED:${RECORDED_PURCHASE_ID}`)
    expect(screen.getByTestId('glance-transaction_value')).toHaveTextContent('EUR 6,186')

    // The recorded price keeps its recorded kind: no basis can be stated for it without a price (the server refuses one).
    const basis = screen.getByLabelText('Price basis')
    expect(basis).toBeDisabled()
    expect(basis).toHaveValue('')
    expect(
      screen.getByText('The recorded purchase price keeps its recorded kind; enter a price to state another basis.'),
    ).toHaveAttribute('role', 'note')

    // A price you enter replaces the recorded one (and its basis); an estimate you enter needs its own source
    // (the server refuses it without one: the recorded source is not reused).
    await user.type(screen.getByLabelText(PRICE_LABEL), '2900')
    expect(basis).toBeEnabled()
    expect(screen.queryByText(/keeps its recorded kind/)).toBeNull()
    expect(screen.getByText('The recorded price basis is not applied to a price you enter.')).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'Keep the recorded (recorded: actual (paid))' })).not.toBeInTheDocument()
    await user.type(screen.getByLabelText(ESTIMATE_LABEL), '4300')
    expect(
      screen.getByText(
        'An estimate you enter needs its own source: the recorded source (SchwackeNet catalogue) is not reused, so the request is refused without one.',
      ),
    ).toHaveAttribute('role', 'note')
    expect(screen.queryByText(/would stay with the estimate/)).toBeNull()
    await user.type(screen.getByLabelText(ESTIMATE_SOURCE_LABEL), 'owner estimate')
    expect(screen.queryByText(/needs its own source/)).toBeNull()
    api.on(ROUTE, () => ok(recordedListingEstimate()))
    await user.click(screen.getByRole('button', { name: 'Calculate' }))
    await waitFor(() => expect(api.callsTo(ROUTE)).toHaveLength(2))
    expect(Object.fromEntries(api.callsTo(ROUTE)[1]!.url.searchParams)).toEqual({
      price_amount: '2900',
      estimated_customs_value_eur: '4300',
      estimated_customs_value_source: 'owner estimate',
      listing_id: LISTING_ID,
    })
  })

  it('never sends a leftover price basis for the recorded purchase price (it keeps its recorded kind)', async () => {
    // Regression: a basis alone relabelled the recorded price (an unpaid estimate shown as an invoice).
    const api = calculatorApi(() => ok(recordedListingEstimate()))
    renderApp(`/candidates/${LISTING_ID}/import-costs`, { api })
    const user = userEvent.setup()
    await screen.findByTestId('estimate-banner')
    const basis = screen.getByLabelText('Price basis')
    expect(basis).toBeDisabled()
    // Typing a price unlocks the basis; clearing it again locks it and drops the chosen basis from the request.
    await user.type(screen.getByLabelText(PRICE_LABEL), '2900')
    await user.selectOptions(basis, 'invoice')
    await user.clear(screen.getByLabelText(PRICE_LABEL))
    expect(basis).toBeDisabled()
    expect(basis).toHaveValue('')
    await user.type(screen.getByLabelText('Mileage (km)'), '181000')
    await user.click(screen.getByRole('button', { name: 'Calculate' }))
    await waitFor(() => expect(api.callsTo(ROUTE)).toHaveLength(2))
    expect(Object.fromEntries(api.callsTo(ROUTE)[1]!.url.searchParams)).toEqual({ mileage_km: '181000', listing_id: LISTING_ID })
  })

  it('marks the estimated customs value fields a validation error names', async () => {
    const api = calculatorApi(() =>
      apiError(422, 'VALIDATION_ERROR', 'Invalid import-cost estimate request', {
        details: { fields: ['estimated_customs_value_source'] },
      }),
    )
    renderApp('/import-costs?estimated_customs_value_eur=4200', { api })
    expect(await screen.findByText('Check: Source of the estimated customs value')).toBeInTheDocument()
    expect(screen.getByLabelText(ESTIMATE_SOURCE_LABEL)).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText(ESTIMATE_LABEL)).toHaveValue('4200')
  })
})
