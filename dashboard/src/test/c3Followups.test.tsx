/**
 * Work package C3 (dashboard follow-ups of the independent reviews). Each test pins one behaviour
 * that the dashboard did not have before C3:
 *
 *  1. the owner's resume sends `expected_removable_suppressions` (the count the owner was shown and
 *     confirmed) and a `suppressions_changed` refusal reloads the controls and shows the new count
 *     before any new attempt;
 *  2. typed waiting reasons (list, detail, attention groups) and the standing-authorization /
 *     configured-sender readiness on the control screen;
 *  3. transient conflicts use `details.reason` (`busy` / `in_progress`), `retryable` as fallback;
 *  4. the reply-signal cap and worker credential state on the reply and health screens;
 *  5. the candidates audit filter `include_screening_rejected`;
 *  6. the activation evidence (read-only; the canary state is not served by the API, never assumed).
 *
 * There is still no approve, send or canary control anywhere.
 */
import { act, renderHook, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ApiResponse } from '../api/client'
import { ApiError, describeError, isTransient, suppressionsChanged, transientReason } from '../api/errors'
import type { InquiryControlView, InquirySummaryView, WaitingReason } from '../api/types'
import { useIdempotentMutation } from '../review/useIdempotentMutation'
import { apiError, deferred, fakeApi, ok, type FakeApi, type Handler } from './fakeApi'
import { candidateSummary, me } from './fixtures'
import { renderApp } from './renderApp'
import {
  control,
  credential,
  gaps,
  health,
  inquiry,
  INQUIRY_ID,
  inquirySummary,
  mailbox,
  reply,
  REPLY_ID,
  replySignals,
  replySummary,
} from './v11Fixtures'

function expectNoApproveOrSendControl() {
  const controls = [...screen.queryAllByRole('button'), ...screen.queryAllByRole('link')]
  expect(controls.length).toBeGreaterThan(0)
  for (const element of controls) {
    const name = (element.textContent ?? '') + ' ' + (element.getAttribute('aria-label') ?? '')
    expect(name).not.toMatch(/\b(approve|approval|send|resend|reply to|answer|canary)\b/i)
  }
}

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
}

function ownerApi(routes: Record<string, Handler>, role: 'owner' | 'reviewer' = 'owner'): FakeApi {
  return fakeApi({ 'GET /api/me': () => ok(me(role)), ...routes })
}

// =================================================================================== 1. resume

interface ResumeBody {
  expected_version: number
  reason: string
  remove_suppressions?: boolean
  expected_removable_suppressions?: number
  idempotency_key: string
}

/** A control world whose server removes exactly what it counts, and refuses a stale count. */
function resumeWorld(initial: InquiryControlView) {
  const current = { value: initial }
  const api = ownerApi({
    'GET /api/inquiry-control': () => ok(current.value),
    'POST /api/inquiry-control/resume': (call) => {
      const body = call.body as ResumeBody
      if (body.expected_version !== current.value.version) {
        return apiError(409, 'VERSION_CONFLICT', 'stale', { details: { current_version: current.value.version } })
      }
      const count = current.value.removable_suppressions
      if (body.remove_suppressions && body.expected_removable_suppressions !== undefined && body.expected_removable_suppressions !== count) {
        return apiError(409, 'VERSION_CONFLICT', 'The suppressions a resume would remove changed; reload the inquiry controls and retry', {
          details: {
            reason: 'suppressions_changed',
            expected_removable_suppressions: body.expected_removable_suppressions,
            current_removable_suppressions: count,
          },
        })
      }
      const removed = body.remove_suppressions ? count : 0
      const bump = current.value.kill_switch ? 1 : 0
      current.value = {
        ...current.value,
        version: current.value.version + bump,
        kill_switch: false,
        kill_switch_reason: null,
        kill_switch_set_at: null,
        removable_suppressions: count - removed,
      }
      return ok({ version: current.value.version, kill_switch: false, mode: 'automatic', resumed_at: '2026-10-07T10:05:00Z', suppressions_removed: removed })
    },
  })
  return { api, current }
}

describe('1. resume with suppression removal sends the confirmed count', () => {
  it('sends expected_removable_suppressions = the count the owner ticked (and none without a removal)', async () => {
    const { api } = resumeWorld(control({ removable_suppressions: 3 }))
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: re-qualify after the check')
    await user.click(screen.getByLabelText(/Also remove the 3 kill-switch/))
    await user.click(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' }))
    expect(await screen.findByTestId('resume-confirmed')).toHaveTextContent('3 suppression(s) removed, each audited.')
    const [sent] = api.callsTo('POST /api/inquiry-control/resume')
    expect(sent?.body).toMatchObject({ expected_version: 3, remove_suppressions: true, expected_removable_suppressions: 3 })
    expectNoApproveOrSendControl()
  })

  it('a resume without the removal sends no count', async () => {
    const { api } = resumeWorld(control({ kill_switch: true, kill_switch_set_at: '2026-10-07T09:00:00Z', kill_switch_reason: 'SYNTHETIC', removable_suppressions: 2 }))
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: resume only')
    await user.click(screen.getByRole('button', { name: 'Resume seller inquiries' }))
    expect(await screen.findByTestId('resume-confirmed')).toHaveTextContent('No suppression was removed.')
    const [sent] = api.callsTo('POST /api/inquiry-control/resume')
    expect(sent?.body).toMatchObject({ remove_suppressions: false })
    expect(sent?.body).not.toHaveProperty('expected_removable_suppressions')
  })

  it('suppressions_changed: reloads, shows the new count, locks until it is shown and needs a new confirmation', async () => {
    const { api, current } = resumeWorld(control({ removable_suppressions: 2 }))
    const reloadGate = { held: false, gate: deferred(), reads: 0 }
    const real = api.fetch.getMockImplementation()!
    api.fetch.mockImplementation(async (input, init) => {
      if (urlOf(input).endsWith('/api/inquiry-control') && (init?.method ?? 'GET') === 'GET') {
        reloadGate.reads += 1
        if (reloadGate.held) await reloadGate.gate.promise
      }
      return real(input, init)
    })
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: remove what I saw')
    await user.click(screen.getByLabelText(/Also remove the 2 kill-switch/))
    // Meanwhile another kill-switch suppression is recorded on the server (count 2 -> 3).
    current.value = { ...current.value, removable_suppressions: 3 }
    reloadGate.held = true
    const controlReads = reloadGate.reads
    await user.click(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' }))

    // The whole resume was refused: nothing removed, nothing resumed, and the reason is explained.
    const rejected = await screen.findByTestId('mutation-rejected')
    expect(rejected).toHaveTextContent('The suppressions a resume would remove changed')
    expect(rejected).toHaveTextContent('You confirmed removing 2 suppression(s), but the server now counts 3')
    expect(within(rejected).getByTestId('error-reason')).toHaveTextContent('suppressions_changed')
    // While the reload is outstanding nothing can be resumed against the stale count.
    expect(await screen.findByTestId('resume-awaiting-reload')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' })).toBeDisabled()
    await waitFor(() => expect(reloadGate.reads).toBeGreaterThan(controlReads))

    reloadGate.gate.release()
    reloadGate.held = false
    // The new set is shown; the ticked confirmation for 2 no longer counts.
    expect(await screen.findByTestId('suppressions-changed')).toHaveTextContent('you confirmed 2, the current count is 3')
    expect(screen.getByTestId('removal-count-moved')).toHaveTextContent('You ticked the removal for 2 suppression(s); the current count is 3')
    expect(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' })).toBeDisabled()
    expect(api.callsTo('POST /api/inquiry-control/resume')).toHaveLength(1)

    // The owner confirms the CURRENT count explicitly: untick, tick.
    const box = screen.getByLabelText(/Also remove the 3 kill-switch/)
    await user.click(box)
    await user.click(box)
    await user.click(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' }))
    expect(await screen.findByTestId('resume-confirmed')).toHaveTextContent('3 suppression(s) removed, each audited.')
    const [first, second] = api.callsTo('POST /api/inquiry-control/resume').map((call) => call.body as ResumeBody)
    expect(first?.expected_removable_suppressions).toBe(2)
    expect(second?.expected_removable_suppressions).toBe(3)
    expect(first?.idempotency_key).not.toBe(second?.idempotency_key)
  })

  it('a count that moves on a manual reload un-confirms the removal until the owner confirms again', async () => {
    const { api, current } = resumeWorld(control({ removable_suppressions: 1 }))
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: check the count')
    await user.click(screen.getByLabelText(/Also remove the 1 kill-switch/))
    current.value = { ...current.value, removable_suppressions: 4 }
    await user.click(screen.getByRole('button', { name: /^Reload/ }))
    expect(await screen.findByTestId('removal-count-moved')).toHaveTextContent('the current count is 4')
    expect(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' })).toBeDisabled()
    expect(api.callsTo('POST /api/inquiry-control/resume')).toHaveLength(0)
  })

  it('suppressionsChanged() reads the counts of the refusal only', () => {
    const refusal = new ApiError({
      code: 'VERSION_CONFLICT',
      message: 'changed',
      status: 409,
      retryable: false,
      details: { reason: 'suppressions_changed', expected_removable_suppressions: 2, current_removable_suppressions: 5 },
    })
    expect(suppressionsChanged(refusal)).toEqual({ expected: 2, current: 5 })
    expect(suppressionsChanged(new ApiError({ code: 'VERSION_CONFLICT', message: 'x', status: 409, retryable: false, details: { current_version: 4 } }))).toBeNull()
  })
})

// ============================================================================ 2. waiting reasons

const WAIT_IDS: Record<WaitingReason, string> = {
  UNCERTAIN_DELIVERY: '66666666-6666-4666-8666-000000000001',
  NEEDS_FACTS: '66666666-6666-4666-8666-000000000002',
  INQUIRIES_PAUSED: '66666666-6666-4666-8666-000000000003',
  SENDER_SETUP_INCOMPLETE: '66666666-6666-4666-8666-000000000004',
  WORKER_OFFLINE: '66666666-6666-4666-8666-000000000005',
  RATE_CAP_REACHED: '66666666-6666-4666-8666-000000000006',
  SELLER_COOLDOWN: '66666666-6666-4666-8666-000000000007',
  SEND_HELD: '66666666-6666-4666-8666-000000000008',
}

function waitingItem(reason: WaitingReason): InquirySummaryView {
  const state =
    reason === 'UNCERTAIN_DELIVERY' ? 'uncertain' : reason === 'NEEDS_FACTS' ? 'held_facts' : reason === 'WORKER_OFFLINE' ? 'sending' : 'queued'
  return inquirySummary({
    inquiry_id: WAIT_IDS[reason],
    vehicle: { ...inquirySummary().vehicle, listing_reference: `SYN-${reason}` },
    state,
    delivery_uncertain: reason === 'UNCERTAIN_DELIVERY',
    accepted_at: null,
    waiting_reason: reason,
  })
}

describe('2. typed waiting reasons and sending readiness', () => {
  it('groups the attention list by typed waiting reason, each inquiry once, with the reason on every row', async () => {
    const all = (Object.keys(WAIT_IDS) as WaitingReason[]).map(waitingItem)
    const api = ownerApi(
      {
        'GET /api/inquiries': (call) =>
          call.url.searchParams.get('attention_only') === 'true' ? ok({ items: all }) : ok({ items: call.url.searchParams.get('state') ? [] : all }),
        'GET /api/inquiry-control': () => ok(control({ used_24h: 2 })),
      },
      'reviewer',
    )
    renderApp('/inquiries', { api })
    expect(await screen.findByTestId('attention-uncertain')).toHaveTextContent('SYN-UNCERTAIN_DELIVERY')
    expect(screen.getByTestId('attention-held')).toHaveTextContent('SYN-NEEDS_FACTS')
    for (const reason of ['INQUIRIES_PAUSED', 'SENDER_SETUP_INCOMPLETE', 'WORKER_OFFLINE', 'RATE_CAP_REACHED', 'SELLER_COOLDOWN', 'SEND_HELD'] as const) {
      const group = screen.getByTestId(`attention-wait-${reason}`)
      expect(within(group).getAllByTestId('inquiry-row')).toHaveLength(1)
      expect(group).toHaveTextContent(`SYN-${reason}`)
      expect(within(group).getByTestId('waiting-reason')).toHaveAttribute('data-reason', reason)
    }
    expect(screen.getByTestId('attention-wait-WORKER_OFFLINE')).toHaveTextContent('Waiting: mail worker offline')
    expect(screen.getByTestId('attention-wait-SELLER_COOLDOWN')).toHaveTextContent('at least 7 days')
    expect(screen.getByTestId('attention-wait-SENDER_SETUP_INCOMPLETE')).toHaveTextContent('not a message approval')
    expect(screen.getByTestId('attention-wait-INQUIRIES_PAUSED')).toHaveTextContent('Waiting: inquiries paused')
    // The worker-offline inquiry is "sending" but waits: never also listed as failed or stuck.
    expect(screen.queryByTestId('attention-failed')).toBeNull()
    // Every inquiry appears exactly once in the attention section.
    const attention = screen.getByRole('region', { name: 'Needs attention' })
    expect(within(attention).getAllByTestId('inquiry-row')).toHaveLength(all.length)
    // The full list names the reason per row too.
    const list = screen.getByRole('table', { name: 'Seller inquiries' })
    expect(within(list).getAllByTestId('waiting-reason').map((el) => el.getAttribute('data-reason'))).toEqual(Object.keys(WAIT_IDS))
    expectNoApproveOrSendControl()
  })

  it('the detail names the waiting reason and explains it is not an approval wait', async () => {
    const api = ownerApi(
      {
        'GET /api/inquiries/:id': () => ok(inquiry({ state: 'sending', waiting_reason: 'WORKER_OFFLINE' })),
        'GET /api/replies': () => ok({ items: [] }),
      },
      'reviewer',
    )
    renderApp(`/inquiries/${INQUIRY_ID}`, { api })
    const notice = await screen.findByTestId('waiting-notice')
    expect(notice).toHaveAttribute('data-reason', 'WORKER_OFFLINE')
    expect(notice).toHaveTextContent("classic Outlook on the owner's PC")
    expect(notice).toHaveTextContent('there is no approval to give and nothing to send by hand')
    expect(within(screen.getByRole('region', { name: 'Status' })).getByTestId('waiting-reason')).toHaveTextContent('mail worker offline')
    expectNoApproveOrSendControl()
  })

  it('a not-waiting inquiry says so', async () => {
    const api = ownerApi({ 'GET /api/inquiries/:id': () => ok(inquiry()), 'GET /api/replies': () => ok({ items: [] }) }, 'reviewer')
    renderApp(`/inquiries/${INQUIRY_ID}`, { api })
    expect(await screen.findByText('not waiting')).toBeInTheDocument()
    expect(screen.queryByTestId('waiting-notice')).toBeNull()
  })

  it('the control screen shows an active authorization and a ready configured sender', async () => {
    const api = ownerApi({ 'GET /api/inquiry-control': () => ok(control()) }, 'reviewer')
    renderApp('/inquiry-control', { api })
    expect(await screen.findByTestId('authorization-status')).toHaveAttribute('data-status', 'active')
    expect(screen.getByTestId('authorization-status')).toHaveTextContent('active version 1')
    expect(screen.getByTestId('sender-readiness')).toHaveAttribute('data-readiness', 'ready')
    expect(screen.getByTestId('sender-readiness')).toHaveTextContent('outlook local · binding version 2')
    expect(screen.getByTestId('sending-state')).toHaveTextContent('the standing authorization is active and the configured sender is ready')
  })

  it('the control screen names a revoked authorization and the configured-identity problems (codes only)', async () => {
    const api = ownerApi(
      {
        'GET /api/inquiry-control': () =>
          ok(
            control({
              authorization_status: 'revoked',
              sender_readiness: 'missing',
              sender_provider: 'outlook_local',
              sender_problems: ['sender_identity_account_not_configured', 'sender_identity_sender_binding_mismatch', 'sender_future_code'],
            }),
          ),
      },
      'reviewer',
    )
    renderApp('/inquiry-control', { api })
    const state = await screen.findByTestId('sending-state')
    expect(state).toHaveTextContent('These controls allow automatic inquiries, but nothing can be sent now')
    expect(state).toHaveTextContent('the standing authorization is revoked')
    expect(state).toHaveTextContent('the configured sender is missing')
    expect(state).toHaveTextContent('never a message approval')
    expect(screen.getByTestId('authorization-status')).toHaveAttribute('data-status', 'revoked')
    const problems = screen.getAllByTestId('sender-problem').map((item) => item.textContent)
    expect(problems).toEqual([
      'sender_identity_account_not_configured (SELLER_EMAIL_ACCOUNT_ID is not configured)',
      'sender_identity_sender_binding_mismatch (the binding is not exactly the configured identity)',
      'sender_future_code',
    ])
    expect(screen.getByRole('main').textContent).not.toMatch(/@/)
  })
})

// ======================================================================= 3. transient reasons

function conflict(details: Record<string, string> | null, retryable: boolean): ApiError {
  return new ApiError({ code: 'VERSION_CONFLICT', message: 'transient', status: 409, retryable, details })
}

describe('3. busy / in_progress conflicts use details.reason', () => {

  it('names in_progress and busy differently; retryable without a reason stays the fallback', () => {
    expect(transientReason(conflict({ reason: 'in_progress' }, true))).toBe('in_progress')
    expect(transientReason(conflict({ reason: 'busy' }, true))).toBe('busy')
    expect(transientReason(conflict(null, true))).toBeNull()
    expect(transientReason(conflict({ reason: 'suppressions_changed' }, false))).toBeNull()
    expect(isTransient(conflict({ reason: 'busy' }, false))).toBe(true)
    expect(isTransient(conflict(null, true))).toBe(true)
    expect(isTransient(conflict(null, false))).toBe(false)
    expect(describeError(conflict({ reason: 'in_progress' }, true)).title).toBe('The same request is still being processed')
    expect(describeError(conflict({ reason: 'in_progress' }, true)).message).toContain('it may still be applied')
    expect(describeError(conflict({ reason: 'busy' }, true), { subject: 'inquiry controls' }).message).toContain(
      'was busy (another operation held it). This request was not applied.',
    )
    expect(describeError(conflict({ reason: 'busy' }, true)).message).not.toContain('still being processed')
    expect(describeError(conflict(null, true)).title).toBe('The server was busy')
  })

  it('in_progress on a FIRST send keeps the outcome unknown (same-key retry only, never a new key)', async () => {
    const api = ownerApi({ 'GET /api/inquiry-control': () => ok(control()) })
    let pauses = 0
    const keys: Array<string | null> = []
    const real = api.fetch.getMockImplementation()!
    api.fetch.mockImplementation(async (input, init) => {
      if (urlOf(input).endsWith('/api/inquiry-control/pause')) {
        pauses += 1
        keys.push(new Headers(init?.headers).get('Idempotency-Key'))
        if (pauses === 1) {
          return apiError(409, 'VERSION_CONFLICT', 'The same request is still in progress; retry shortly', {
            retryable: true,
            details: { reason: 'in_progress' },
          })
        }
        return ok({ version: 4, kill_switch: true, already_paused: false, kill_switch_set_at: '2026-10-07T10:00:00Z', mode: 'automatic', notice: 'Seller inquiries paused. Resuming requires a separate owner action.' })
      }
      return real(input, init)
    })
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Pause reason/), 'SYNTHETIC: in progress elsewhere')
    await user.click(screen.getByRole('button', { name: 'Pause seller inquiries' }))
    const unconfirmed = await screen.findByTestId('mutation-unconfirmed')
    expect(unconfirmed).toHaveTextContent('Not confirmed: it may or may not have been saved')
    expect(unconfirmed).toHaveTextContent('The server is still processing an identical request with the same key')
    expect(screen.getByTestId('retry-in-progress')).toHaveTextContent('may still be applied')
    expect(screen.queryByTestId('mutation-rejected')).toBeNull()
    // No new attempt (new key) is possible while it is unknown.
    expect(screen.getByRole('button', { name: 'Pause seller inquiries' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
    expect(await screen.findByTestId('pause-confirmed')).toBeInTheDocument()
    expect(keys).toHaveLength(2)
    expect(keys[0]).toBe(keys[1])
  })

  it('a retry refused with reason busy stays unconfirmed even when the retryable flag is missing', async () => {
    const send = vi.fn<(body: { note: string; idempotency_key: string }) => Promise<ApiResponse<{ ok: true }>>>()
    send
      .mockRejectedValueOnce(new ApiError({ code: 'NETWORK_ERROR', message: 'lost', status: null, retryable: true, outcomeUnknown: true }))
      .mockRejectedValueOnce(
        new ApiError({ code: 'VERSION_CONFLICT', message: 'busy', status: 409, retryable: false, details: { reason: 'busy' } }),
      )
    const { result } = renderHook(() => useIdempotentMutation<{ note: string; idempotency_key: string }, { ok: true }>('note', send))
    await act(async () => result.current.submit({ note: 'SYNTHETIC note' }))
    await act(async () => result.current.retry())
    expect(result.current.phase.kind).toBe('unconfirmed')
  })

  it('a busy FIRST send is definitive (rolled back) and says busy, not "changed"', async () => {
    const api = ownerApi({
      'GET /api/inquiry-control': () => ok(control()),
      'POST /api/inquiry-control/pause': () =>
        apiError(409, 'VERSION_CONFLICT', 'The record is busy; retry shortly', { retryable: true, details: { reason: 'busy' } }),
    })
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Pause reason/), 'SYNTHETIC: busy row')
    await user.click(screen.getByRole('button', { name: 'Pause seller inquiries' }))
    const rejected = await screen.findByTestId('mutation-rejected')
    expect(rejected).toHaveTextContent('The server was busy')
    expect(rejected).toHaveTextContent('was busy (another operation held it). This request was not applied.')
    expect(within(rejected).getByTestId('error-reason')).toHaveTextContent('busy')
  })
})

// ================================================================ 4. signal cap and credentials

describe('4. reply-signal cap and worker credential state', () => {
  it('the reply list and detail show the signal state; a rate-limited reply explains it woke no dot', async () => {
    const api = ownerApi(
      {
        'GET /api/replies': () =>
          ok({
            items: [
              replySummary({ signal_status: 'rate_limited' }),
              replySummary({ reply_id: '77777777-7777-4777-8777-777777777778', signal_status: 'coalesced' }),
              replySummary({ reply_id: '77777777-7777-4777-8777-777777777779', signal_status: null }),
            ],
          }),
        'GET /api/replies/:id': () => ok(reply({ signal_status: 'rate_limited' })),
      },
      'reviewer',
    )
    renderApp('/replies', { api })
    const table = await screen.findByRole('table', { name: 'Seller replies' })
    expect(within(table).getAllByTestId('signal-status').map((el) => el.getAttribute('data-signal'))).toEqual(['rate_limited', 'coalesced'])
    expect(within(table).getByText('signal cap reached')).toBeInTheDocument()
    expect(within(table).getByText('not recorded')).toBeInTheDocument()
    renderApp(`/replies/${REPLY_ID}`, { api })
    const notice = await screen.findByTestId('signal-rate-limited')
    expect(notice).toHaveTextContent('the reply is stored and shown here, but it started no new dot activation')
    expectNoApproveOrSendControl()
  })

  it('health shows each credential, an expired credential of an active worker as a gap, revoked workers and the signal cap', async () => {
    const box = mailbox()
    const api = ownerApi(
      {
        'GET /api/mail-workers/health': () =>
          ok(
            health([box], {
              revoked_mailboxes: 2,
              credentials: [credential({ credential_status: 'expired', expires_at: '2026-10-06T00:00:00Z' })],
              reply_signals: replySignals({ emitted: 7, coalesced: 3, rate_limited: 2, inquiries_at_cap: 1 }),
            }),
          ),
        'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
        'GET /api/inquiry-control': () => ok(control()),
      },
      'reviewer',
    )
    renderApp('/mail-workers', { api })
    expect(await screen.findByTestId('credentials-not-live')).toHaveTextContent('1 active worker(s) hold an expired or revoked credential')
    expect(screen.getByTestId('mailbox-credential-not-live')).toHaveTextContent('Its credential is expired')
    const row = screen.getByTestId('credential-row')
    expect(row).toHaveAttribute('data-status', 'expired')
    expect(screen.getByTestId('revoked-mailboxes')).toHaveTextContent('2 revoked mail worker(s) are not listed')
    const signals = screen.getByTestId('reply-signals')
    expect(screen.getByTestId('signals-rate-limited')).toHaveTextContent('2 reply signal(s) hit the per-inquiry cap in the last 24 h')
    expect(within(signals).getByTestId('signal-cap')).toHaveTextContent('6 per 24 h PROPOSED')
    expect(signals).toHaveTextContent('Coalesced into a pending signal3')
    // No token, hash or prefix is ever rendered.
    expect(document.body.textContent).not.toMatch(/suvmail_|token/i)
  })

  it('a revoked worker listed on request says it can never upload again', async () => {
    const revoked = mailbox({ binding_state: 'revoked', monitoring_active: false, heartbeat_status: 'down' })
    const api = ownerApi(
      {
        'GET /api/mail-workers/health': () =>
          ok(health([revoked], { revoked_mailboxes: 1, credentials: [credential({ binding_state: 'revoked', credential_status: 'revoked', revoked_at: '2026-10-07T08:00:00Z' })] })),
        'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
        'GET /api/inquiry-control': () => ok(control()),
      },
      'reviewer',
    )
    renderApp('/mail-workers', { api })
    expect(await screen.findByTestId('mailbox-revoked')).toHaveTextContent('can never upload replies or claim sends again')
    expect(screen.getByTestId('credential-row')).toHaveAttribute('data-status', 'revoked')
    // A revoked worker's revoked credential is not reported as a live-worker gap.
    expect(screen.queryByTestId('credentials-not-live')).toBeNull()
  })
})

// =================================================================== 5. candidates audit filter

describe('5. candidates: include screening-rejected (audit)', () => {
  it('keeps the audit filter in the URL and the API query, labels rejected rows and keeps it for the next page', async () => {
    const rejected = candidateSummary({
      listing_id: 'bbbbbbbb-0000-4000-8000-0000000000f1',
      title: 'SYNTHETIC screening-rejected Trail',
      eligibility: 'rejected',
      review_state: null,
      case_id: null,
    })
    const api = ownerApi(
      {
        'GET /api/candidates': (call) =>
          call.url.searchParams.get('include_screening_rejected') === 'true'
            ? ok({ items: [candidateSummary(), rejected] }, 200, { next_cursor: call.url.searchParams.get('cursor') ? null : 'cursor-audit-2' })
            : ok({ items: [candidateSummary()] }),
      },
      'reviewer',
    )
    const { router } = renderApp('/candidates', { api })
    const user = userEvent.setup()
    await screen.findByRole('table', { name: 'Candidates' })
    expect(screen.queryByTestId('audit-filter-notice')).toBeNull()
    await user.click(screen.getByLabelText(/include screening-rejected \(audit\)/))
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(router.state.location.search).toBe('?include_screening_rejected=true'))
    expect(await screen.findByTestId('audit-filter-notice')).toHaveTextContent('kept for audit only and are not candidates')
    const flagged = await screen.findByTestId('screening-rejected-badge')
    expect(flagged).toHaveTextContent('screening rejected (audit)')
    expect(screen.getAllByTestId('candidate-row').filter((row) => row.getAttribute('data-screening-rejected') === 'yes')).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Load more' }))
    await waitFor(() =>
      expect(
        api
          .callsTo('GET /api/candidates')
          .some((call) => call.url.searchParams.get('cursor') === 'cursor-audit-2' && call.url.searchParams.get('include_screening_rejected') === 'true'),
      ).toBe(true),
    )
    await user.click(screen.getByRole('button', { name: 'Clear' }))
    await waitFor(() => expect(router.state.location.search).toBe(''))
    expect(screen.getByLabelText(/include screening-rejected \(audit\)/)).not.toBeChecked()
  })

  it('a URL with the audit filter opens with it applied', async () => {
    const api = ownerApi({ 'GET /api/candidates': () => ok({ items: [] }) }, 'reviewer')
    renderApp('/candidates?include_screening_rejected=true', { api })
    expect(await screen.findByTestId('audit-filter-notice')).toBeInTheDocument()
    expect(screen.getByLabelText(/include screening-rejected \(audit\)/)).toBeChecked()
    expect(api.callsTo('GET /api/candidates')[0]?.url.searchParams.get('include_screening_rejected')).toBe('true')
  })
})

// ======================================================================= 6. activation evidence

function activationStates(): Record<string, string | null> {
  return Object.fromEntries(screen.getAllByTestId('activation-row').map((row) => [row.getAttribute('data-evidence'), row.getAttribute('data-state')]))
}

describe('6. activation evidence on the health screen (read-only)', () => {
  it('shows the readiness rows from the API and the canary rows as not shown here, with no canary or send control', async () => {
    const off = mailbox({ monitoring_active: false, heartbeat_status: 'down', open_gap_count: 1 })
    const api = ownerApi({
      'GET /api/mail-workers/health': () => ok(health([off])),
      'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
      'GET /api/inquiry-control': () => ok(control({ authorization_status: 'not_effective', sender_readiness: 'ready' })),
    })
    renderApp('/mail-workers', { api })
    await waitFor(() => expect(activationStates()).toEqual({ sender: 'done', runtime: 'open', authorization: 'open', canary: 'not_shown' }))
    const canary = screen.getByTestId('canary-evidence')
    expect(canary).toHaveTextContent('does not report the activation canary yet')
    expect(canary).toHaveTextContent('never assumed')
    expect(canary).toHaveTextContent('suv-deals canary status')
    const section = screen.getByRole('region', { name: 'Activation evidence (read-only)' })
    expect(within(section).queryAllByRole('button')).toHaveLength(0)
    expect(within(section).queryAllByRole('link')).toHaveLength(0)
    expectNoApproveOrSendControl()
    // Only reads: the health screen never writes.
    expect(api.calls.filter((call) => call.method !== 'GET')).toEqual([])
  })

  it('without inquiry controls the readiness rows are unknown, never met', async () => {
    const api = ownerApi({
      'GET /api/mail-workers/health': () => ok(health([mailbox()])),
      'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
      'GET /api/inquiry-control': () => apiError(404, 'NOT_FOUND', 'Seller inquiry controls not found'),
    })
    renderApp('/mail-workers', { api })
    await waitFor(() => expect(activationStates()).toEqual({ sender: 'unknown', runtime: 'done', authorization: 'unknown', canary: 'not_shown' }))
    await waitFor(() => expect(api.callsTo('GET /api/inquiry-control')).toHaveLength(1))
    expect(screen.queryByRole('alert', { name: /Not found/ })).toBeNull()
  })
})


// ============================================================ C3 review 1 (safety / privacy lens)

describe('C3 review: a removal confirmation never outlives a change of the removable set', () => {
  it('a suppressions_changed refusal voids the confirmation even when the reloaded count is the same again (a swapped set)', async () => {
    const { api, current } = resumeWorld(control({ removable_suppressions: 2 }))
    const real = api.fetch.getMockImplementation()!
    api.fetch.mockImplementation(async (input, init) => {
      const response = await real(input, init)
      // Right after the refusal another suppression is removed elsewhere: the count is 2 again, but
      // the set is not the one the owner ticked (one was added, another removed).
      if (urlOf(input).endsWith('/api/inquiry-control/resume')) current.value = { ...current.value, removable_suppressions: 2 }
      return response
    })
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: remove what I saw')
    await user.click(screen.getByLabelText(/Also remove the 2 kill-switch/))
    current.value = { ...current.value, removable_suppressions: 3 } // one added before the click
    await user.click(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' }))

    expect(await screen.findByTestId('suppressions-changed')).toHaveTextContent('you confirmed 2, the current count is 2')
    // The server said the set changed: the old tick must be given again before anything is resumed.
    expect(screen.getByTestId('removal-count-moved')).toHaveTextContent('may not be the same set')
    expect(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' })).toBeDisabled()
    expect(api.callsTo('POST /api/inquiry-control/resume')).toHaveLength(1)

    const box = screen.getByLabelText(/Also remove the 2 kill-switch/)
    await user.click(box)
    await user.click(box)
    await user.click(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' }))
    expect(await screen.findByTestId('resume-confirmed')).toHaveTextContent('2 suppression(s) removed, each audited.')
    const [first, second] = api.callsTo('POST /api/inquiry-control/resume').map((call) => call.body as ResumeBody)
    expect(first?.expected_removable_suppressions).toBe(2)
    expect(second?.expected_removable_suppressions).toBe(2)
    expect(first?.idempotency_key).not.toBe(second?.idempotency_key)
  })

  it('a count that moved away and back on reloads still needs a new confirmation', async () => {
    const { api, current } = resumeWorld(control({ removable_suppressions: 1 }))
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Resume reason/), 'SYNTHETIC: check the count')
    await user.click(screen.getByLabelText(/Also remove the 1 kill-switch/))
    current.value = { ...current.value, removable_suppressions: 2 }
    await user.click(screen.getByRole('button', { name: /^Reload/ }))
    expect(await screen.findByTestId('removal-count-moved')).toHaveTextContent('the current count is 2')
    current.value = { ...current.value, removable_suppressions: 1 }
    await user.click(screen.getByRole('button', { name: /^Reload/ }))
    expect(await screen.findByLabelText(/Also remove the 1 kill-switch/)).toBeChecked()
    // Back at 1, but not necessarily the suppression the owner ticked.
    expect(screen.getByTestId('removal-count-moved')).toHaveTextContent('may not be the same set')
    expect(screen.getByRole('button', { name: 'Re-qualify suppressed inquiries' })).toBeDisabled()
    expect(api.callsTo('POST /api/inquiry-control/resume')).toHaveLength(0)
  })
})

describe('C3 review: a worker whose credential is not live is never shown as monitoring', () => {
  it.each([
    ['expired', { credential_status: 'expired' as const, expires_at: '2026-10-07T09:58:00Z' }],
    ['revoked', { credential_status: 'revoked' as const, revoked_at: '2026-10-07T09:58:00Z' }],
  ])('a fresh heartbeat with an %s credential is a coverage gap, not monitoring', async (_status, cred) => {
    // The server still reports monitoring_active (its last heartbeat is fresh), but the worker can
    // no longer upload replies or claim sends with its credential.
    const api = ownerApi(
      {
        'GET /api/mail-workers/health': () => ok(health([mailbox()], { credentials: [credential(cred)] })),
        'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
        'GET /api/inquiry-control': () => ok(control()),
      },
      'reviewer',
    )
    renderApp('/mail-workers', { api })
    expect(await screen.findByTestId('mailbox-credential-not-live')).toBeInTheDocument()
    expect(screen.getByTestId('monitoring-summary')).toHaveTextContent('No mailbox is monitored right now')
    expect(screen.getByTestId('mailbox-card')).toHaveAttribute('data-monitoring', 'no')
    expect(screen.queryByText('monitoring', { exact: true })).toBeNull()
    await waitFor(() => expect(activationStates()).toMatchObject({ runtime: 'open' }))
    expect(screen.getByRole('region', { name: 'Activation evidence (read-only)' })).not.toHaveTextContent('a mailbox is monitored')
  })

  it('an active credential with a fresh heartbeat stays monitoring', async () => {
    const api = ownerApi(
      {
        'GET /api/mail-workers/health': () => ok(health([mailbox()])),
        'GET /api/mail-workers/coverage-gaps': () => ok(gaps([])),
        'GET /api/inquiry-control': () => ok(control()),
      },
      'reviewer',
    )
    renderApp('/mail-workers', { api })
    expect(await screen.findByTestId('monitoring-summary')).toHaveTextContent('At least one mailbox is monitored right now')
    expect(screen.getByTestId('mailbox-card')).toHaveAttribute('data-monitoring', 'yes')
    await waitFor(() => expect(activationStates()).toMatchObject({ runtime: 'done' }))
  })
})
