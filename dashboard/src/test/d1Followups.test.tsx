/**
 * Work package D1 (dashboard). Each test pins one behaviour the dashboard did not have before D1:
 *
 *  6. the owner's activation rows 4-6 come from the read-only `GET /api/activation/canary-evidence`
 *     (met only when the server says `complete`; other roles never request it; no canary control);
 *  7. the inquiry control reports the backend's PROCESS-level gate (`SELLER_INQUIRY_MODE`, the
 *     process kill switch, the owner's message-approval setting) next to the database controls,
 *     and never claims automatic inquiries are possible while the server says they are not.
 */
import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { ActivationCanaryView, CanaryEvidenceView } from '../api/types'
import { fakeApi, ok, type FakeApi, type Handler } from './fakeApi'
import { me } from './fixtures'
import { renderApp } from './renderApp'
import { control, gaps, health, inquirySummary, mailbox } from './v11Fixtures'

function api(routes: Record<string, Handler>, role: 'owner' | 'reviewer' = 'owner'): FakeApi {
  return fakeApi({ 'GET /api/me': () => ok(me(role)), ...routes })
}

function canaryItem(overrides: Partial<ActivationCanaryView> = {}): ActivationCanaryView {
  return {
    id: '7c0d5a8e-1f00-4c1a-9a51-6b1a2c3d4e5f',
    provider: 'outlook_local',
    sender_binding_version: 2,
    current_sender_version: true,
    state: 'prepared',
    created_at: '2026-10-07T08:00:00Z',
    outcome_recorded_at: null,
    accepted_at: null,
    reply_recorded_at: null,
    ...overrides,
  }
}

function evidence(overrides: Partial<CanaryEvidenceView> = {}): CanaryEvidenceView {
  return {
    evidence: 'prepared',
    detail: 'newest canary prepared (sender binding v2); no correlated reply',
    sender_provider: 'outlook_local',
    sender_binding_version: 2,
    canaries: [canaryItem()],
    notes: ['Read-only: a canary is prepared and sent only by the owner on the command line (suv-deals canary).'],
    ...overrides,
  }
}

function healthRoutes(extra: Record<string, Handler> = {}): Record<string, Handler> {
  return {
    'GET /api/mail-workers/health': () => ok(health([mailbox()])),
    'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
    'GET /api/inquiry-control': () => ok(control()),
    ...extra,
  }
}

function canaryRow(): HTMLElement {
  return screen.getAllByTestId('activation-row').find((row) => row.getAttribute('data-evidence') === 'canary')!
}

// ======================================================================= 6. canary evidence

describe('D1 item 6: the owner sees the canary evidence rows 4-6 from the API (read-only)', () => {
  it('a prepared canary is open, listed by id and state only, with no canary or send control', async () => {
    const fake = api(healthRoutes({ 'GET /api/activation/canary-evidence': () => ok(evidence()) }))
    renderApp('/mail-workers', { api: fake })
    await waitFor(() => expect(canaryRow()).toHaveAttribute('data-state', 'open'))
    expect(screen.getByTestId('canary-evidence')).toHaveAttribute('data-evidence-state', 'prepared')
    expect(screen.getByTestId('canary-evidence')).toHaveTextContent('prepared: not sent yet')
    const [item] = screen.getAllByTestId('canary-item')
    expect(item).toHaveAttribute('data-state', 'prepared')
    expect(item).toHaveTextContent('binding v2')
    const section = screen.getByRole('region', { name: 'Activation evidence (read-only)' })
    expect(within(section).queryAllByRole('button')).toHaveLength(0)
    expect(within(section).queryAllByRole('link')).toHaveLength(0)
    expect(section).toHaveTextContent('sent only by the owner on the command line')
    expect(fake.calls.filter((call) => call.method !== 'GET')).toEqual([])
  })

  it('only a server-reported complete evidence is shown as met; an older-version canary is marked', async () => {
    const fake = api(
      healthRoutes({
        'GET /api/activation/canary-evidence': () =>
          ok(
            evidence({
              evidence: 'complete',
              detail: 'correlated test reply recorded (sender binding v2)',
              canaries: [
                canaryItem({ state: 'reply_correlated', accepted_at: '2026-10-07T08:05:00Z', reply_recorded_at: '2026-10-07T08:30:00Z' }),
                canaryItem({ id: '1c0d5a8e-1f00-4c1a-9a51-6b1a2c3d4e5f', state: 'failed', sender_binding_version: 1, current_sender_version: false }),
              ],
            }),
          ),
      }),
    )
    renderApp('/mail-workers', { api: fake })
    await waitFor(() => expect(canaryRow()).toHaveAttribute('data-state', 'done'))
    const items = screen.getAllByTestId('canary-item')
    expect(items.map((item) => item.getAttribute('data-state'))).toEqual(['reply_correlated', 'failed'])
    expect(items[0]).toHaveTextContent('test reply')
    expect(items[1]).toHaveTextContent('(older version)')
  })

  it.each(['stale', 'none', 'no_sender', 'uncertain'] as const)('evidence %s is open, never met', async (state) => {
    const fake = api(healthRoutes({ 'GET /api/activation/canary-evidence': () => ok(evidence({ evidence: state, canaries: [] })) }))
    renderApp('/mail-workers', { api: fake })
    await waitFor(() => expect(canaryRow()).toHaveAttribute('data-state', 'open'))
    expect(screen.getByTestId('canary-evidence')).toHaveAttribute('data-evidence-state', state)
  })

  it('another role never requests the owner-only evidence and sees it as owner only', async () => {
    const fake = api(healthRoutes({ 'GET /api/activation/canary-evidence': () => ok(evidence({ evidence: 'complete' })) }), 'reviewer')
    renderApp('/mail-workers', { api: fake })
    await waitFor(() => expect(canaryRow()).toHaveAttribute('data-state', 'owner_only'))
    expect(screen.getByTestId('canary-evidence')).toHaveTextContent('shown to the owner only')
    await waitFor(() => expect(fake.callsTo('GET /api/inquiry-control')).toHaveLength(1))
    expect(fake.callsTo('GET /api/activation/canary-evidence')).toHaveLength(0)
  })
})

// ======================================================================= 7. process gate

describe('D1 item 7: the control screen reports the process-level gate and never over-claims', () => {
  it('a closed process gate means nothing can be sent although the database says automatic', async () => {
    const closed = control({
      process_mode: 'disabled_until_sender_ready',
      process_blockers: ['SELLER_INQUIRY_MODE_NOT_AUTOMATIC'],
      automatic_inquiries_possible: false,
    })
    const fake = api({ 'GET /api/inquiry-control': () => ok(closed) }, 'reviewer')
    renderApp('/inquiry-control', { api: fake })
    const state = await screen.findByTestId('sending-state')
    expect(state).toHaveTextContent('These controls allow automatic inquiries, but nothing can be sent now')
    expect(state).toHaveTextContent('SELLER_INQUIRY_MODE is disabled until sender ready, not automatic')
    expect(state).not.toHaveTextContent('the backend process gate is open')
    expect(screen.getByTestId('process-gate')).toHaveAttribute('data-open', 'no')
    expect(screen.getByTestId('automatic-possible')).toHaveAttribute('data-possible', 'no')
  })

  it.each([
    [{ process_kill_switch: true, process_blockers: ['SELLER_INQUIRY_KILL_SWITCH_ON' as const] }, 'process kill switch (SELLER_INQUIRY_KILL_SWITCH) is on'],
    [
      { process_message_approval_required: true, process_blockers: ['MESSAGE_APPROVAL_SETTING_ON' as const] },
      'SELLER_INQUIRY_REQUIRE_MESSAGE_APPROVAL disables automatic sending',
    ],
    [{ process_mode: null }, 'did not report its process gate'],
    [{}, 'the server does not report automatic inquiries as possible'],
  ])('a closed gate (%o) is named, never claimed open', async (overrides, text) => {
    const view = control({ ...overrides, automatic_inquiries_possible: false })
    const fake = api({ 'GET /api/inquiry-control': () => ok(view) }, 'reviewer')
    renderApp('/inquiry-control', { api: fake })
    const state = await screen.findByTestId('sending-state')
    expect(state).toHaveTextContent('nothing can be sent now')
    expect(state).toHaveTextContent(text)
  })

  it.each([
    // The activation runbook holds real inquiries with caps of 0 while the process gate is open.
    [{ max_per_24h: 0, max_per_15d: 0, used_24h: 0, used_15d: 0 }, "the owner's rolling caps are 0 (every seller inquiry is held)"],
    [{ max_per_24h: 2, used_24h: 2 }, 'the rolling 24-hour cap is used up (2 of 2)'],
    [{ max_per_15d: 5, used_15d: 5, used_24h: 0 }, 'the rolling 15-day cap is used up (5 of 5)'],
  ])('room under the owner caps (%o) is named, never claimed possible', async (overrides, text) => {
    const view = control({ ...overrides, automatic_inquiries_possible: false })
    const fake = api({ 'GET /api/inquiry-control': () => ok(view) }, 'reviewer')
    renderApp('/inquiry-control', { api: fake })
    const state = await screen.findByTestId('sending-state')
    expect(state).toHaveTextContent('nothing can be sent now')
    expect(state).toHaveTextContent(text)
    expect(state).not.toHaveTextContent('the server does not report automatic inquiries as possible')
    expect(screen.getByTestId('automatic-possible')).toHaveAttribute('data-possible', 'no')
  })

  it('every gate open: the server says possible and the screen says so', async () => {
    const fake = api({ 'GET /api/inquiry-control': () => ok(control()) }, 'reviewer')
    renderApp('/inquiry-control', { api: fake })
    const state = await screen.findByTestId('sending-state')
    expect(state).toHaveTextContent('These controls allow automatic inquiries: the backend process gate is open')
    expect(screen.getByTestId('process-gate')).toHaveAttribute('data-open', 'yes')
    expect(screen.getByTestId('automatic-possible')).toHaveAttribute('data-possible', 'yes')
  })

  it('the inquiry list summary does not imply sending while the server says it is not possible', async () => {
    const closed = control({ process_mode: 'paused', process_blockers: ['SELLER_INQUIRY_MODE_NOT_AUTOMATIC'], automatic_inquiries_possible: false })
    const fake = api(
      {
        'GET /api/inquiry-control': () => ok(closed),
        'GET /api/inquiries': () => ok({ items: [inquirySummary()] }),
      },
      'reviewer',
    )
    renderApp('/inquiries', { api: fake })
    expect(await screen.findByTestId('control-summary')).toHaveTextContent('nothing can be sent now')
  })
})
