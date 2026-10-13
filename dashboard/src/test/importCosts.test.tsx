import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { apiError, fakeApi, ok } from './fakeApi'
import { candidateDetail, LISTING_ID, me } from './fixtures'
import { BANNER_EN, BANNER_MK, importEstimate, incompleteScenario, listingEstimate, previewScenario } from './importCostFixtures'
import { renderApp } from './renderApp'
import { formFromSearch, queryFromSearch, searchFromForm } from '../screens/importCosts/form'

const ROUTE = 'GET /api/import-costs/estimate'

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
    await user.type(screen.getByLabelText('Purchase price'), '3000.00')
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
    const totals = screen.getByTestId('totals-legal_base')
    expect(totals).toHaveTextContent('Indicative total import charges (MKD)')
    expect(totals).toHaveTextContent('MKD 168,154.45')
    expect(totals).toHaveTextContent('EUR 2,732.72')
    expect(totals).toHaveTextContent('Indicative landed cost (EUR)')
    expect(totals).toHaveTextContent('EUR 6,432.72')
    expect(totals).not.toHaveTextContent(/^Total import charges/)
    // Helper components are secondary rows, never counted in a total.
    const components = screen.getByRole('table', { name: 'Components (legal_base)' })
    const helper = within(components).getByText('customs_value_mkd').closest('tr')!
    expect(helper).toHaveClass('row-intermediate')
    expect(within(helper).getByText(/intermediate · not counted in the total/)).toBeInTheDocument()
    const duty = within(components).getByText('duty_standard').closest('tr')!
    expect(duty).not.toHaveClass('row-intermediate')
    expect(within(duty).getByText('MKD 11,383.73')).toBeInTheDocument()
    const categories = screen.getByRole('table', { name: 'Import charges by category (legal_base)' })
    expect(within(categories).getByText('motor vehicle tax').closest('tr')).toHaveTextContent('MKD 96,390.00')
    // The cost profile's transport line, as the server states it (the browser adds nothing to it).
    const basis = screen.getByRole('region', { name: 'Basis' })
    expect(within(basis).getByText('Transport (cost profile)').nextElementSibling).toHaveTextContent('700.00 EUR per vehicle')
    expect(screen.getByText(/Euro 5 meets the Euro 4 minimum/)).toBeInTheDocument()
    expect(screen.getByRole('list', { name: 'Uncertain items' }).firstElementChild).toHaveTextContent('applies')
    expect(screen.getByText(/tax rules unapproved/)).toBeInTheDocument() // the envelope warning
  })

  it('shows unknown charges as "unknown", never as 0, with the known subtotal labelled as not a total', async () => {
    const view = importEstimate({ scenarios: [incompleteScenario()], missing_facts: ['co2'] })
    const api = calculatorApi(() => ok(view, 200, { warnings: [{ code: 'UNKNOWN_COSTS', message: 'Some import charges are unknown (never zero); needed: co2' }] }))
    renderApp('/import-costs?fuel=diesel&price_amount=3000.00&price_currency=EUR', { api })
    const totals = await screen.findByTestId('totals-legal_base')
    const rows = within(totals).getAllByRole('definition')
    expect(rows[0]).toHaveTextContent(/^unknown/)
    expect(rows[1]).toHaveTextContent(/^unknown/)
    expect(rows[2]).toHaveTextContent(/^unknown/)
    expect(totals).toHaveTextContent('Known subtotal (not a total)')
    expect(totals).not.toHaveTextContent(/MKD 0(\.00)?\b/)
    const components = screen.getByRole('table', { name: 'Components (legal_base)' })
    for (const id of ['dmv_full', 'vat_import']) {
      const row = within(components).getByText(id).closest('tr')!
      expect(within(row).getByText('unknown', { selector: '.amount' })).toBeInTheDocument()
      expect(row.textContent).not.toMatch(/0\.00/)
    }
    expect(screen.getByText('Needed to complete the calculation:')).toBeInTheDocument()
    expect(screen.getByText(/Unknown charges are\s+never counted as zero/)).toBeInTheDocument()
  })

  it('names the fields a validation error points at and never claims a result', async () => {
    const api = calculatorApi(() =>
      apiError(422, 'VALIDATION_ERROR', 'Invalid import-cost estimate request', { details: { fields: ['price_currency'] } }),
    )
    renderApp('/import-costs?price_amount=3000', { api })
    expect(await screen.findByText('Check: Price currency')).toBeInTheDocument()
    expect(screen.getAllByRole('alert').some((node) => node.textContent?.includes('VALIDATION_ERROR'))).toBe(true)
    expect(screen.getByLabelText('Price currency')).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByLabelText('Purchase price')).not.toHaveAttribute('aria-invalid')
    expect(screen.getByLabelText('Purchase price')).toHaveValue('3000')
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
    expect(screen.getByText('From the listing: EUR 3,000.00')).toBeInTheDocument()
    expect(screen.getByText('From the listing: 189 g/km (nedc)')).toBeInTheDocument()
    expect(screen.getByLabelText('Purchase price')).toHaveAttribute('placeholder', 'EUR 3,000.00')
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
    const totals = await screen.findByTestId('totals-legal_base')
    expect(totals).toHaveTextContent('Total import charges (MKD)')
    expect(totals).toHaveTextContent('MKD 168,154.45')
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
    expect(screen.queryByTestId('totals-legal_base')).toBeNull()
    expect(screen.getByText('tax rule set')).toBeInTheDocument()
  })
})
