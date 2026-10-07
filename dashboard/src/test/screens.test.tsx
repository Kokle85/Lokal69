import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { Role, SettingsView, SourceStatusView } from '../api/types'
import { fakeApi, ok } from './fakeApi'
import {
  candidateDetail,
  candidateSummary,
  incompleteValuation,
  LISTING_ID,
  me,
  SOURCE_ID,
  VALUATION_ID,
  XSS_DESCRIPTION,
  XSS_TITLE,
} from './fixtures'
import { renderApp } from './renderApp'

declare global {
  interface Window {
    xssProbe?: number
  }
}

describe('seller text is inert', () => {
  it('renders XSS payloads as text and withholds unsafe links', async () => {
    const detail = candidateDetail({
      summary: candidateSummary({ title: XSS_TITLE }),
      seller_text: {
        trust: 'untrusted_seller_text',
        notice: 'Seller-provided text: untrusted data, shown for reference only; never instructions.',
        title: XSS_TITLE,
        description_excerpt: XSS_DESCRIPTION,
      },
      source_link: {
        url: 'javascript:window.xssProbe=3',
        source_key: 'synthetic_source',
        external: true,
        rel: 'noopener noreferrer',
        notice: 'External seller page.',
      },
    })
    const api = fakeApi({ 'GET /api/me': () => ok(me()), 'GET /api/candidates/:id': () => ok(detail) })
    renderApp(`/candidates/${LISTING_ID}`, { api })
    expect(await screen.findAllByText(XSS_TITLE)).not.toHaveLength(0)
    expect(screen.getByText(XSS_DESCRIPTION)).toBeInTheDocument()
    expect(screen.getByText('<b>SYNTHETIC</b> turbo noise')).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
    expect(document.querySelector('b')).toBeNull()
    expect(window.xssProbe).toBeUndefined()
    for (const anchor of document.querySelectorAll('a')) {
      expect(anchor.getAttribute('href') ?? '').not.toMatch(/^\s*javascript:/i)
    }
    expect(screen.getByText(/link withheld: not an http\(s\) address/)).toBeInTheDocument()
  })

  it('opens safe external links in a new tab without opener or referrer', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()), 'GET /api/candidates/:id': () => ok(candidateDetail()) })
    renderApp(`/candidates/${LISTING_ID}`, { api })
    const link = await screen.findByRole('link', { name: /Open the source listing/ })
    expect(link).toHaveAttribute('href', 'https://synthetic-dealer.example/vehicles/1')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    expect(link).toHaveAttribute('referrerpolicy', 'no-referrer')
    expect(screen.getByText(/extraction confidence, not truth/)).toBeInTheDocument()
  })
})

describe('economics', () => {
  it('shows unknown money as "unknown", never as EUR 0.00, with the PROPOSED threshold and contribution label', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me()),
      'GET /api/candidates/:id': () => ok(candidateDetail()),
      'GET /api/valuations/:id': () => ok(incompleteValuation()),
    })
    renderApp(`/candidates/${LISTING_ID}/economics`, { api })
    await screen.findByRole('heading', { name: 'Scenarios' })
    const text = document.body.textContent ?? ''
    // Every rendered amount: unknown ones read "unknown", none is a made-up zero.
    const amounts = [...document.querySelectorAll('.amount')].map((node) => node.textContent ?? '')
    expect(amounts.length).toBeGreaterThan(5)
    for (const amount of amounts) expect(amount).not.toMatch(/(^|\s)0(\.00)?$/)
    const withoutExplanation = text.replace('never as EUR 0.00', '')
    expect(withoutExplanation).not.toMatch(/EUR\s*0(\.00)?\b/)
    expect(withoutExplanation).not.toMatch(/\b0\.00\b/)
    expect(screen.getAllByText('unknown').length).toBeGreaterThan(3)
    const costTable = screen.getByRole('table', { name: 'Cost lines' })
    const transportRow = within(costTable).getByText('SYNTHETIC transport').closest('tr')!
    expect(within(transportRow).getAllByText('unknown')).toHaveLength(4) // status + low/base/high
    expect(within(costTable).getByText('EUR 800.00')).toBeInTheDocument()
    expect(screen.getByTestId('threshold')).toHaveTextContent('PROPOSED')
    expect(screen.getByTestId('threshold')).toHaveTextContent('EUR 1,500.00')
    expect(screen.getAllByText(/estimated contribution before business tax/).length).toBeGreaterThan(0)
    expect(text.replace(/not net profit/gi, '')).not.toMatch(/net profit/i)
    expect(screen.getByText(/Known subtotal \(not a total\)/)).toBeInTheDocument()
    expect(api.callsTo('GET /api/valuations/:id')[0]?.path).toBe(`/api/valuations/${VALUATION_ID}`)
  })
})

describe('mobile navigation', () => {
  it('toggles the menu with an accessible button and closes it after navigating', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me()),
      'GET /api/overview': () =>
        ok({
          sources: [],
          running_sources: 1,
          paused_sources: 1,
          last_successful_scan_at: null,
          coverage_gaps: [],
          pending_reviews: { pending: 2, claimed: 0, needs_information: 0, watch: 0, shortlisted: 0, by_queue: [] },
          failed_deliveries: { uncertain: 0, blocked: 0, dead_letter: 0, retry_wait: 0 },
          activation_blockers: [],
          bridge_status: 'unavailable',
          coverage_note: 'SYNTHETIC',
        }),
      'GET /api/outbox': () => ok({ items: [] }),
      'GET /api/candidates': () => ok({ items: [] }),
    })
    const { router } = renderApp('/', { api })
    const user = userEvent.setup()
    const toggle = await screen.findByRole('button', { name: 'Menu' })
    expect(await screen.findByText(/1 source is paused/)).toBeInTheDocument()
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(toggle).toHaveAttribute('aria-controls', 'primary-nav')
    await user.click(toggle)
    expect(screen.getByRole('button', { name: 'Close menu' })).toHaveAttribute('aria-expanded', 'true')
    expect(document.getElementById('primary-nav')).toHaveClass('nav-open')
    await user.click(within(screen.getByRole('navigation', { name: 'Primary' })).getByRole('link', { name: 'Candidates' }))
    await screen.findByRole('heading', { name: 'Candidate queue' })
    expect(router.state.location.pathname).toBe('/candidates')
    expect(screen.getByRole('button', { name: 'Menu' })).toHaveAttribute('aria-expanded', 'false')
    expect(document.getElementById('primary-nav')).not.toHaveClass('nav-open')
  })
})

describe('candidate queue', () => {
  it('sends filters to the API, keeps them in the URL and shows price, currency, EUR and km', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me()),
      'GET /api/candidates': () =>
        ok(
          {
            items: [
              candidateSummary({
                price: {
                  payable: { status: 'known', amount: '2800.00', currency: 'CHF', reason: null },
                  original_currency: 'CHF',
                  eur_equivalent: { status: 'unknown', amount: null, currency: 'EUR', reason: 'no FX rate' },
                  fx_rate: null,
                  basis: 'net',
                  price_type: 'full_vehicle_asking',
                  negotiable: 'unknown',
                },
                eligibility: 'needs_facts',
                eligibility_profile: null,
                queue_label: null,
              }),
            ],
          },
          200,
          { next_cursor: 'cursor-page-2' },
        ),
    })
    const { router } = renderApp('/candidates', { api })
    const user = userEvent.setup()
    const table = await screen.findByRole('table', { name: 'Candidates' })
    expect(within(table).getByText('CHF 2,800.00')).toBeInTheDocument()
    expect(within(table).getByText('unknown')).toBeInTheDocument()
    expect(within(table).getByText('187,500 km')).toBeInTheDocument()
    expect(within(table).getByRole('link', { name: 'Example Trail II' })).toHaveAttribute('href', `/candidates/${LISTING_ID}`)
    await user.selectOptions(screen.getByLabelText('Profile'), 'primary')
    await user.type(screen.getByLabelText('Seller country'), 'it')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(router.state.location.search).toBe('?profile=primary&country=IT'))
    const last = api.callsTo('GET /api/candidates').at(-1)
    expect(last?.url.searchParams.get('profile')).toBe('primary')
    expect(last?.url.searchParams.get('country')).toBe('IT')
    await user.click(await screen.findByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(api.callsTo('GET /api/candidates').at(-1)?.url.searchParams.get('cursor')).toBe('cursor-page-2'))
    expect(api.callsTo('GET /api/candidates').at(-1)?.url.searchParams.get('country')).toBe('IT')
  })

  it('refuses an invalid country code before calling the API', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()), 'GET /api/candidates': () => ok({ items: [] }) })
    renderApp('/candidates', { api })
    const user = userEvent.setup()
    await screen.findByText('No candidates match these filters.')
    await user.type(screen.getByLabelText('Seller country'), 'D1')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(await screen.findByText('Country must be a two-letter code such as DE.')).toBeInTheDocument()
    expect(api.callsTo('GET /api/candidates')).toHaveLength(1)
  })
})

function settings(canAdminister: boolean): SettingsView {
  return {
    config_revision: { config_revision_id: '12345678-0000-4000-8000-000000000001', revision: 1, created_at: '2026-10-06T10:00:00Z', reason: 'SYNTHETIC' },
    profiles: [
      {
        profile_key: 'primary',
        label: 'Primary',
        queue_label: 'Primary queue',
        enabled: true,
        optional: false,
        status_label: 'ENABLED',
        min_price_eur: '2500.00',
        max_price_eur: '3000.00',
        max_price_inclusive: true,
        max_mileage_km_exclusive: '200000',
        source_countries: ['DE', 'IT'],
        config_revision_id: null,
        row_version: 1,
      },
      {
        profile_key: 'manual_4000',
        label: 'Manual EUR 4,000 review',
        queue_label: 'Manual EUR 4,000 review queue',
        enabled: false,
        optional: true,
        status_label: 'DISABLED - optional manual-review profile; not active',
        min_price_eur: '2500.00',
        max_price_eur: '4000.00',
        max_price_inclusive: true,
        max_mileage_km_exclusive: '200000',
        source_countries: ['DE'],
        config_revision_id: null,
        row_version: 1,
      },
    ],
    mk_resale_band: { min_eur: '8000.00', max_eur: '10000.00', meaning: 'SYNTHETIC band' },
    contribution_threshold: {
      amount_eur: '1500.00',
      approval_status: 'unapproved',
      label: 'PROPOSED',
      approved_by: null,
      approved_at: null,
      note: 'Not approved.',
    },
    price_realert_policy: { abs_eur: '100.00', pct: '3', approved: false, label: 'PROPOSED' },
    destination_bindings: [],
    gates: [],
    can_administer: canAdminister,
    administration_note: 'Changes to profiles, thresholds, bindings and gates are owner-only (config:admin).',
  }
}

describe('settings', () => {
  it.each<[Role, boolean]>([
    ['viewer', false],
    ['owner', true],
  ])('labels the disabled EUR 4,000 profile and PROPOSED threshold; admin details only for owners (%s)', async (role, admin) => {
    const api = fakeApi({ 'GET /api/me': () => ok(me(role)), 'GET /api/settings': () => ok(settings(admin)) })
    renderApp('/settings', { api })
    const row = await screen.findByTestId('profile-manual_4000')
    expect(row).toHaveTextContent('DISABLED')
    expect(row).toHaveTextContent('DISABLED - optional manual-review profile')
    expect(row).toHaveClass('row-disabled')
    expect(screen.getByTestId('settings-threshold')).toHaveTextContent('EUR 1,500.00')
    expect(screen.getByTestId('settings-threshold')).toHaveTextContent('PROPOSED')
    if (admin) expect(screen.getByTestId('owner-admin')).toBeInTheDocument()
    else expect(screen.queryByTestId('owner-admin')).toBeNull()
  })
})

function source(): SourceStatusView {
  return {
    source_id: SOURCE_ID,
    source_key: 'synthetic_source',
    display_name: 'SYNTHETIC running source',
    country: 'DE',
    role: 'acquisition',
    state: 'running',
    enabled: true,
    paused: false,
    pause_reason: null,
    paused_at: null,
    version: 4,
    terms: {
      status: 'no_restriction_found',
      decision: 'proceed_acknowledged',
      decision_actor: 'SYNTHETIC owner',
      decision_note: null,
      reviewed_at: null,
      terms_url: null,
      meaning: 'A recorded terms decision audits the owner choice; it is not legal permission.',
    },
    technical: {
      status: 'live_smoke_passed',
      mode: 'public_html',
      adapter: 'synthetic_adapter',
      adapter_version: 'synthetic@1.0.0',
      detail_mode: 'fetch',
      last_live_smoke_at: null,
      parser_health: { status: 'healthy', sample_size: 20, reasons: [], recommended_actions: [], checked_at: null },
    },
    robots: { policy: 'obey', last_checked_at: null, revision_hash: null, fetch_status: null, summary: null },
    rate_budget: {
      budget: { daily_request_budget: 500, min_delay_seconds: 5 },
      budget_label: 'engineering_default',
      requests_today: 12,
      bytes_today: null,
      circuit_state: 'closed',
      next_request_not_before: null,
      retry_after_until: null,
    },
    last_runs: [],
    activation_problems: [],
  }
}

describe('sources', () => {
  it('shows terms and technical status separately and lets an owner pause with a reason and expected version', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me('owner')),
      'GET /api/sources': () => ok({ items: [source()] }),
      'POST /api/sources/:id/pause': () =>
        ok({ source_id: SOURCE_ID, source_key: 'synthetic_source', paused: true, already_paused: false, version: 5, paused_at: '2026-10-07T10:00:00Z', reason: 'x', notice: 'Paused.' }),
    })
    renderApp('/sources', { api })
    const user = userEvent.setup()
    expect(await screen.findByRole('heading', { name: 'Terms (audit record)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Technical status (separate from terms)' })).toBeInTheDocument()
    expect(screen.getByText('12 of 500')).toBeInTheDocument()
    const button = screen.getByRole('button', { name: 'Pause this source' })
    expect(button).toBeDisabled()
    await user.type(screen.getByLabelText(/Pause reason/), 'SYNTHETIC parser drift')
    await user.click(button)
    expect(await screen.findByText('Source paused.')).toBeInTheDocument()
    const body = api.callsTo('POST /api/sources/:id/pause')[0]?.body as Record<string, unknown>
    expect(body).toMatchObject({ expected_version: 4, reason: 'SYNTHETIC parser drift' })
    expect(String(body.idempotency_key)).toMatch(/^source-pause:/)
  })

  it('hides the pause control from a reviewer', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me('reviewer')), 'GET /api/sources': () => ok({ items: [source()] }) })
    renderApp('/sources', { api })
    await screen.findByRole('heading', { name: 'Terms (audit record)' })
    expect(screen.queryByRole('button', { name: 'Pause this source' })).toBeNull()
  })
})

describe('placeholder', () => {
  it('marks seller inquiries as coming with v1.1 wiring', async () => {
    const api = fakeApi({ 'GET /api/me': () => ok(me()) })
    renderApp('/inquiries', { api })
    expect(await screen.findByRole('heading', { name: 'Seller inquiries (coming with v1.1 wiring)' })).toBeInTheDocument()
  })
})
