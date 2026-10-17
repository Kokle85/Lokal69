/**
 * Settings: the owner's "Tax rules and cost profile" section (owner approval path, contract 10.2).
 * Every rule set, hash, name and amount here is SYNTHETIC; the fake API stands in for the backend.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type {
  CostProfileApproveResult,
  CostProfileSummaryView,
  CostProfilesView,
  Role,
  SettingsView,
  TaxRuleChangeResult,
  TaxRuleSetSummaryView,
  TaxRuleSetsView,
  TaxRuleStatus,
} from '../api/types'
import { apiError, fakeApi, ok, type Handler } from './fakeApi'
import { me } from './fixtures'
import { renderApp } from './renderApp'

const HASH = 'a'.repeat(64)
const PROFILE_HASH = 'b'.repeat(64)
const LABEL = 'mk-passenger-car-import@2026.10-draft.3'
const FILE = 'mk_passenger_car_import_2026.10-draft.3.json'
const UNCERTAIN = 'UNCERTAIN - confirm: СКД = customs monthly EUR/MKD rate, CO2 band edges and non-integer CO2.'

function settings(): SettingsView {
  return {
    config_revision: null,
    profiles: [],
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
    can_administer: true,
    administration_note: 'Changes are owner-only (config:admin).',
  }
}

function ruleSet(overrides: Partial<TaxRuleSetSummaryView> = {}): TaxRuleSetSummaryView {
  return {
    origin: 'draft_file',
    file_name: FILE,
    rule_set_id: 'mk-passenger-car-import',
    version: '2026.10-draft.3',
    label: LABEL,
    jurisdiction: 'MK',
    vehicle_categories: ['passenger_car'],
    status: 'draft',
    is_fixture: false,
    valid_from: '2025-07-01',
    valid_to: null,
    currency: 'MKD',
    description: 'DRAFT, NOT APPROVED. SYNTHETIC description.',
    content_sha256: HASH,
    sha256_sealed: false,
    sources: [{ url: 'https://example.invalid/law.pdf', title: 'SYNTHETIC law', retrieved_at: '2026-10-13T12:00:00Z', sha256: 'c'.repeat(64) }],
    components: [
      {
        id: 'duty_standard',
        label: 'Customs duty, standard',
        category: 'import_duty',
        kind: 'percentage',
        intermediate: false,
        legal_reference: null,
        uncertain: false,
      },
    ],
    uncertain_items: [UNCERTAIN],
    approval: null,
    allowed_actions: ['store'],
    production_selectable: false,
    created_at: null,
    updated_at: null,
    ...overrides,
  }
}

function stored(status: TaxRuleStatus, allowed: TaxRuleSetSummaryView['allowed_actions']): TaxRuleSetSummaryView {
  return ruleSet({
    origin: 'stored',
    file_name: null,
    status,
    sha256_sealed: status !== 'draft',
    allowed_actions: allowed,
    created_at: '2026-10-13T08:00:00Z',
    updated_at: '2026-10-13T08:00:00Z',
  })
}

function rules(rows: TaxRuleSetSummaryView[], mode: TaxRuleSetsView['calculator']['mode'] = 'indicative_preview'): TaxRuleSetsView {
  return {
    rule_sets: rows,
    file_notes: [],
    calculator: {
      jurisdiction: 'MK',
      vehicle_category: 'passenger_car',
      on_date: '2026-10-13',
      mode,
      rule_set: LABEL,
      status: rows[0]?.status ?? null,
      reason: 'SYNTHETIC selection reason',
    },
    production_note: 'Valuations and alerts use only an ACTIVE, owner-approved rule set.',
  }
}

function profile(overrides: Partial<CostProfileSummaryView> = {}): CostProfileSummaryView {
  const transport = {
    category: 'transport' as const,
    label: 'Transport to North Macedonia',
    status: 'estimated' as const,
    currency: 'EUR',
    low: '700.00',
    base: '700.00',
    high: '700.00',
    note: 'Owner decision: a fixed EUR 700 per vehicle.',
    scope: null,
  }
  return {
    origin: 'configured_file',
    id: null,
    profile_key: 'default',
    version: 3,
    basis: 'SYNTHETIC basis',
    currency: 'EUR',
    approval_status: 'not_stored',
    approved_at: null,
    approved_by_principal: null,
    is_fixture: false,
    content_sha256: PROFILE_HASH,
    matches_stored: null,
    transport,
    lines: [
      transport,
      { ...transport, category: 'repairs', label: 'Repairs', status: 'unknown', low: null, base: null, high: null, note: 'Unknown.' },
    ],
    can_approve: true,
    created_at: null,
    ...overrides,
  }
}

function profiles(configured: CostProfileSummaryView = profile()): CostProfilesView {
  return {
    configured_file: 'cost_profiles/default_unapproved.yaml',
    configured,
    configured_problem: null,
    profiles: [],
    note: 'SYNTHETIC cost profile note.',
  }
}

function change(overrides: Partial<TaxRuleChangeResult> = {}): TaxRuleChangeResult {
  return {
    action: 'store',
    rule_set_id: 'mk-passenger-car-import',
    version: '2026.10-draft.3',
    label: LABEL,
    previous_status: null,
    status: 'draft',
    content_sha256: HASH,
    sha256_sealed: false,
    approved_by: null,
    approved_at: null,
    changed_at: '2026-10-13T09:00:00Z',
    production_effect: 'Stored as a draft: NOT approved.',
    ...overrides,
  }
}

function ownerApi(routes: Record<string, Handler>, role: Role = 'owner') {
  return fakeApi({
    'GET /api/me': () => ok(me(role)),
    'GET /api/settings': () => ok({ ...settings(), can_administer: role === 'owner' }),
    'GET /api/settings/cost-profiles': () => ok(profiles()),
    ...routes,
  })
}

describe('settings: tax rules and cost profile (owner)', () => {
  it('shows the draft with its status chip, hash, UNCERTAIN items and the review reminder', async () => {
    const api = ownerApi({ 'GET /api/settings/tax-rules': () => ok(rules([ruleSet()])) })
    renderApp('/settings', { api })
    const row = await screen.findByTestId(`rule-set-${LABEL}`)
    expect(row).toHaveTextContent('draft')
    expect(row).toHaveTextContent('repository draft')
    expect(row).toHaveTextContent('1 UNCERTAIN')
    expect(within(row).getByRole('button', { name: 'Store draft' })).toBeEnabled()
    expect(screen.getByTestId('tax-rules-reminder')).toHaveTextContent('resolve every UNCERTAIN item')
    expect(screen.getByTestId(`uncertain-${LABEL}`)).toHaveTextContent(UNCERTAIN)
    expect(screen.getByTestId('calculator-selection')).toHaveTextContent('indicative preview')
    expect(api.callsTo('POST /api/settings/tax-rules/store')).toHaveLength(0) // nothing happens by itself
  })

  it('stores the draft only after the owner reads the effect and confirms it', async () => {
    let isStored = false
    const api = ownerApi({
      'GET /api/settings/tax-rules': () => ok(rules([isStored ? stored('draft', ['under_review', 'revoked']) : ruleSet()])),
      'POST /api/settings/tax-rules/store': () => {
        isStored = true
        return ok(change())
      },
    })
    renderApp('/settings', { api })
    const user = userEvent.setup()
    await user.click(within(await screen.findByTestId(`rule-set-${LABEL}`)).getByRole('button', { name: 'Store draft' }))
    const form = screen.getByTestId('tax-rule-action-form')
    expect(within(form).getByTestId('production-effect')).toHaveTextContent('production valuations and alerts do not change')
    const submit = within(form).getByRole('button', { name: 'Store draft' })
    expect(submit).toBeDisabled()
    await user.click(within(form).getByRole('checkbox'))
    await user.click(submit)
    await waitFor(() => expect(api.callsTo('POST /api/settings/tax-rules/store')).toHaveLength(1))
    const call = api.callsTo('POST /api/settings/tax-rules/store')[0]!
    const body = call.body as Record<string, unknown>
    expect(body).toMatchObject({ file_name: FILE, content_sha256: HASH })
    expect(call.headers.get('Idempotency-Key')).toBe(body.idempotency_key)
    expect(await screen.findByTestId('tax-rule-result')).toHaveTextContent('NOT approved')
    await waitFor(() =>
      expect(within(screen.getByTestId(`rule-set-${LABEL}`)).getByRole('button', { name: 'Submit for review' })).toBeInTheDocument(),
    )
  })

  it('approves only with a named approver and a review record bound to the content hash', async () => {
    const api = ownerApi({
      'GET /api/settings/tax-rules': () => ok(rules([stored('under_review', ['approved', 'revoked'])])),
      'POST /api/settings/tax-rules/transition': () =>
        ok(change({ action: 'approved', previous_status: 'under_review', status: 'approved', approved_by: 'SYNTHETIC owner' })),
    })
    renderApp('/settings', { api })
    const user = userEvent.setup()
    await user.click(within(await screen.findByTestId(`rule-set-${LABEL}`)).getByRole('button', { name: 'Approve' }))
    const form = screen.getByTestId('tax-rule-action-form')
    expect(within(form).getByTestId('production-effect')).toHaveTextContent('activating it WILL change them')
    await user.click(within(form).getByRole('checkbox'))
    const submit = within(form).getByRole('button', { name: 'Approve' })
    expect(submit).toBeDisabled() // no approver, reviewer or scope yet
    expect(within(form).getByTestId('action-problems')).toHaveTextContent('Name the approver.')
    await user.type(within(form).getByLabelText('Approver name (recorded with the approval)'), 'SYNTHETIC owner')
    await user.type(within(form).getByLabelText('Reviewer'), 'SYNTHETIC broker')
    await user.type(within(form).getByLabelText(/What the review verified/), 'Every component, source and golden case')
    await user.type(within(form).getByLabelText(/Evidence references/), 'SYNTHETIC-ref-1{enter}SYNTHETIC-ref-2')
    const hash = within(form).getByLabelText(/Reviewed content hash/)
    await user.clear(hash)
    await user.type(hash, 'd'.repeat(64))
    expect(within(form).getByTestId('action-problems')).toHaveTextContent('does not cover this content')
    expect(submit).toBeDisabled()
    await user.clear(hash)
    await user.type(hash, HASH)
    expect(within(form).getByTestId('approval-record-length')).toHaveTextContent('of 500 characters')
    await waitFor(() => expect(submit).toBeEnabled())
    await user.click(submit)
    await waitFor(() => expect(api.callsTo('POST /api/settings/tax-rules/transition')).toHaveLength(1))
    const body = api.callsTo('POST /api/settings/tax-rules/transition')[0]!.body as Record<string, unknown>
    expect(body).toMatchObject({
      rule_set_id: 'mk-passenger-car-import',
      version: '2026.10-draft.3',
      expected_status: 'under_review',
      target: 'approved',
      content_sha256: HASH,
      approved_by: 'SYNTHETIC owner',
      review_record: {
        reviewer: 'SYNTHETIC broker',
        scope: 'Every component, source and golden case',
        evidence_refs: ['SYNTHETIC-ref-1', 'SYNTHETIC-ref-2'],
        content_sha256: HASH,
      },
    })
    const record = (body.review_record ?? {}) as Record<string, unknown>
    expect(String(record.reviewed_at)).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/)
    expect(await screen.findByTestId('tax-rule-result')).toHaveTextContent('under review → approved')
  })

  it('says that activating and revoking change production valuations', async () => {
    const api = ownerApi({ 'GET /api/settings/tax-rules': () => ok(rules([stored('approved', ['active', 'revoked'])])) })
    renderApp('/settings', { api })
    const user = userEvent.setup()
    const row = await screen.findByTestId(`rule-set-${LABEL}`)
    await user.click(within(row).getByRole('button', { name: 'Activate' }))
    expect(screen.getByTestId('production-effect')).toHaveTextContent('from now on PRODUCTION valuations')
    await user.click(within(row).getByRole('button', { name: 'Revoke' }))
    expect(screen.getByTestId('production-effect')).toHaveTextContent('Production valuations that used it are marked stale')
    expect(within(screen.getByTestId('tax-rule-action-form')).getByRole('button', { name: 'Revoke' })).toBeDisabled()
  })

  it('shows a refused transition (status changed) and sends nothing else', async () => {
    const api = ownerApi({
      'GET /api/settings/tax-rules': () => ok(rules([stored('draft', ['under_review', 'revoked'])])),
      'POST /api/settings/tax-rules/transition': () =>
        apiError(409, 'VERSION_CONFLICT', 'The tax rule set status changed; reload and retry', {
          details: { reason: 'status_changed', expected_status: 'draft', current_status: 'under_review' },
        }),
    })
    renderApp('/settings', { api })
    const user = userEvent.setup()
    await user.click(within(await screen.findByTestId(`rule-set-${LABEL}`)).getByRole('button', { name: 'Submit for review' }))
    const form = screen.getByTestId('tax-rule-action-form')
    await user.click(within(form).getByRole('checkbox'))
    await user.click(within(form).getByRole('button', { name: 'Submit for review' }))
    expect(await screen.findByTestId('mutation-rejected')).toHaveTextContent('status_changed')
    expect(api.callsTo('POST /api/settings/tax-rules/transition')).toHaveLength(1)
    const body = api.callsTo('POST /api/settings/tax-rules/transition')[0]!.body as Record<string, unknown>
    expect(body).toMatchObject({ expected_status: 'draft', target: 'under_review', content_sha256: HASH })
    expect(body).not.toHaveProperty('approved_by')
    expect(body).not.toHaveProperty('review_record')
  })

  it('shows the fixed EUR 700 transport and approves the cost profile after confirmation', async () => {
    let approved = false
    const result: CostProfileApproveResult = {
      id: '22222222-2222-4222-8222-222222222222',
      profile_key: 'default',
      version: 3,
      approval_status: 'approved',
      approved_at: '2026-10-13T09:00:00Z',
      content_sha256: PROFILE_HASH,
      stored_now: true,
      production_effect: 'Approved: valuations that used the unapproved version are marked stale.',
    }
    const api = ownerApi({
      'GET /api/settings/tax-rules': () => ok(rules([])),
      'GET /api/settings/cost-profiles': () =>
        ok(profiles(approved ? profile({ approval_status: 'approved', can_approve: false, id: result.id, matches_stored: true }) : profile())),
      'POST /api/settings/cost-profiles/approve': () => {
        approved = true
        return ok(result)
      },
    })
    renderApp('/settings', { api })
    const user = userEvent.setup()
    expect(await screen.findByTestId('cost-profile-transport')).toHaveTextContent('EUR 700.00 per vehicle')
    const form = screen.getByTestId('cost-profile-approve-form')
    expect(within(form).getByTestId('cost-profile-effect')).toHaveTextContent('Production valuations that used the unapproved version')
    expect(within(form).getByTestId('cost-profile-effect')).toHaveTextContent('stored first, then approved')
    const submit = within(form).getByRole('button', { name: 'Approve cost profile' })
    expect(submit).toBeDisabled()
    await user.click(within(form).getByRole('checkbox'))
    await user.click(submit)
    await waitFor(() => expect(api.callsTo('POST /api/settings/cost-profiles/approve')).toHaveLength(1))
    const body = api.callsTo('POST /api/settings/cost-profiles/approve')[0]!.body as Record<string, unknown>
    expect(body).toMatchObject({ profile_key: 'default', version: 3, content_sha256: PROFILE_HASH })
    expect(await screen.findByTestId('cost-profile-result')).toHaveTextContent('version 3 approved')
    await waitFor(() => expect(screen.queryByTestId('cost-profile-approve-form')).toBeNull())
  })

  it.each<Role>(['reviewer', 'viewer'])('shows %s only a read-only note and requests nothing owner-only', async (role) => {
    const api = ownerApi({}, role)
    renderApp('/settings', { api })
    expect(await screen.findByTestId('tax-rules-owner-only')).toHaveTextContent('workspace owner only')
    expect(api.callsTo('GET /api/settings/tax-rules')).toHaveLength(0)
    expect(api.callsTo('GET /api/settings/cost-profiles')).toHaveLength(0)
    expect(screen.queryByRole('button', { name: /Store draft|Approve|Activate|Revoke/ })).toBeNull()
  })
})
