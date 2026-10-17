/**
 * Independent security review (spec v1.1 dashboard): regression tests for the safety, privacy and
 * security findings. Each test failed on the code before the fix it pins.
 */
import { act, renderHook, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { ApiResponse } from '../api/client'
import { ApiError } from '../api/errors'
import type { InquiryControlView } from '../api/types'
import { useIdempotentMutation } from '../review/useIdempotentMutation'
import { readPendingControl } from '../screens/inquiries/controlMarker'
import { apiError, fakeApi, ok, type FakeApi } from './fakeApi'
import { TEST_USER_ID } from './fakeAuth'
import { me, WORKSPACE_ID } from './fixtures'
import { renderApp } from './renderApp'
import { control, inquiry, INQUIRY_ID, REPLY_ID, reply, replySummary } from './v11Fixtures'

function urlOf(input: RequestInfo | URL): string {
  return typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
}

function ownerControlApi(initial: InquiryControlView = control()): { api: FakeApi; current: { value: InquiryControlView } } {
  const current = { value: initial }
  const api = fakeApi({
    'GET /api/me': () => ok(me('owner')),
    'GET /api/inquiry-control': () => ok(current.value),
    'POST /api/inquiry-control/pause': (call) => {
      const body = call.body as { expected_version: number; reason: string }
      current.value = {
        ...current.value,
        version: body.expected_version + 1,
        kill_switch: true,
        kill_switch_reason: body.reason,
        kill_switch_set_at: '2026-10-07T10:00:00Z',
      }
      return ok({
        version: current.value.version,
        kill_switch: true,
        already_paused: false,
        kill_switch_set_at: '2026-10-07T10:00:00Z',
        mode: 'automatic',
        notice: 'Seller inquiries paused. Resuming requires a separate owner action.',
      })
    },
  })
  return { api, current }
}

// ------------------------------------------------------------------------------------------------
// Outcome-unknown mutations: a retry turned away as BUSY says nothing about the original send.
//
// The backend answers `409 VERSION_CONFLICT` with `retryable: true` when the transaction could not
// run (lock timeout, serialization failure, deadlock: `errors_map.TransientConflict`) and when the
// same idempotency key is still in progress. Such an answer to a RETRY proves only that the retry
// was rolled back; the original request may still commit. Treating it as a definitive rejection
// ("Nothing was saved") unlocked a NEW attempt with a NEW key: a second write (two notes, two
// rechecks, a second re-qualifying resume) and a false "not saved" for a pause/resume that applies.
// ------------------------------------------------------------------------------------------------

describe('a retry refused as busy keeps an unknown outcome unknown', () => {
  it('inquiry pause: the attempt stays unconfirmed (same key, form locked, marker kept) until a retry is answered', async () => {
    const { api } = ownerControlApi()
    const real = api.fetch.getMockImplementation()!
    let pauses = 0
    const sent: Array<{ key: string | null; body: unknown }> = []
    api.fetch.mockImplementation(async (input, init) => {
      if (urlOf(input).endsWith('/api/inquiry-control/pause')) {
        pauses += 1
        sent.push({ key: new Headers(init?.headers).get('Idempotency-Key'), body: init?.body })
        if (pauses === 1) throw new TypeError('Failed to fetch') // the response is lost
        if (pauses === 2) {
          return apiError(409, 'VERSION_CONFLICT', 'The same request is still in progress; retry shortly', { retryable: true })
        }
      }
      return real(input, init)
    })
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Pause reason/), 'SYNTHETIC: stop now')
    await user.click(screen.getByRole('button', { name: 'Pause seller inquiries' }))
    expect(await screen.findByTestId('mutation-unconfirmed')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
    await waitFor(() => expect(pauses).toBe(2))
    // Still unknown: never "Nothing was saved", never a fresh attempt with a new key.
    expect(await screen.findByTestId('mutation-unconfirmed')).toHaveTextContent('Not confirmed: it may or may not have been saved')
    expect(screen.getByTestId('retry-busy')).toHaveTextContent('still unconfirmed')
    expect(screen.queryByTestId('mutation-rejected')).toBeNull()
    expect(screen.getByRole('button', { name: 'Pause seller inquiries' })).toBeDisabled()
    expect(readPendingControl(WORKSPACE_ID, TEST_USER_ID)?.action).toBe('pause')

    await user.click(screen.getByRole('button', { name: 'Retry the same request' }))
    expect(await screen.findByTestId('pause-confirmed')).toHaveTextContent('Seller inquiries paused.')
    expect(sent).toHaveLength(3)
    expect(new Set(sent.map((call) => call.key)).size).toBe(1)
    expect(new Set(sent.map((call) => call.body)).size).toBe(1)
  })

  it('a note (no expected version): after a busy retry no second note can be sent with a new key', async () => {
    const send = vi.fn<(body: { note: string; idempotency_key: string }) => Promise<ApiResponse<{ ok: true }>>>()
    send
      .mockRejectedValueOnce(
        new ApiError({ code: 'NETWORK_ERROR', message: 'lost', status: null, retryable: true, outcomeUnknown: true }),
      )
      .mockRejectedValueOnce(
        new ApiError({ code: 'VERSION_CONFLICT', message: 'Concurrent update; retry the request', status: 409, retryable: true }),
      )
    const { result } = renderHook(() => useIdempotentMutation<{ note: string; idempotency_key: string }, { ok: true }>('note', send))
    await act(async () => result.current.submit({ note: 'SYNTHETIC first note' }))
    expect(result.current.phase.kind).toBe('unconfirmed')
    await act(async () => result.current.retry())
    expect(result.current.phase.kind).toBe('unconfirmed')
    await act(async () => result.current.submit({ note: 'SYNTHETIC second note' }))
    expect(send).toHaveBeenCalledTimes(2)
    expect(send.mock.calls[1]?.[0]).toEqual(send.mock.calls[0]?.[0])
  })

  it('a definitive refusal of a retry (a real, non-retryable conflict) still settles the attempt', async () => {
    const send = vi.fn<(body: { note: string; idempotency_key: string }) => Promise<ApiResponse<{ ok: true }>>>()
    send
      .mockRejectedValueOnce(
        new ApiError({ code: 'NETWORK_ERROR', message: 'lost', status: null, retryable: true, outcomeUnknown: true }),
      )
      .mockRejectedValueOnce(new ApiError({ code: 'VERSION_CONFLICT', message: 'The object changed', status: 409, retryable: false }))
    const { result } = renderHook(() => useIdempotentMutation<{ note: string; idempotency_key: string }, { ok: true }>('note', send))
    await act(async () => result.current.submit({ note: 'SYNTHETIC note' }))
    await act(async () => result.current.retry())
    expect(result.current.phase.kind).toBe('rejected')
  })

  it('a busy refusal of a FIRST send is reported as busy, not as "the controls changed"', async () => {
    const { api } = ownerControlApi()
    api.on('POST /api/inquiry-control/pause', () =>
      apiError(409, 'VERSION_CONFLICT', 'The record is busy; retry shortly', { retryable: true }),
    )
    renderApp('/inquiry-control', { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText(/Pause reason/), 'SYNTHETIC: busy row')
    await user.click(screen.getByRole('button', { name: 'Pause seller inquiries' }))
    const rejected = await screen.findByTestId('mutation-rejected')
    expect(rejected).toHaveTextContent('busy')
    expect(rejected).not.toHaveTextContent('changed since you loaded them')
  })
})

// ------------------------------------------------------------------------------------------------
// The owner's Slack links must open the reply: the backend builds
// `<dashboard>/inquiries/<inquiry_id>/replies/<reply_id>` (domain.replies.dashboard_reply_url) for
// the seller-reply signal AND the "decision needed" owner alert.
// ------------------------------------------------------------------------------------------------

describe('Slack deep links', () => {
  it('opens the reply from the seller-reply signal / owner-alert URL instead of "Page not found"', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me('owner')),
      'GET /api/replies/:id': () => ok(reply()),
    })
    renderApp(`/inquiries/${INQUIRY_ID}/replies/${REPLY_ID}`, { api })
    expect(await screen.findByRole('heading', { name: /^Reply: SYN-TIGUAN-1/, level: 1 })).toBeInTheDocument()
    expect(screen.getByTestId('escalations')).toHaveTextContent('Needs your decision')
    expect(screen.queryByRole('heading', { name: 'Page not found' })).toBeNull()
    expect(api.callsTo('GET /api/replies/:id').map((call) => call.path)).toEqual([`/api/replies/${REPLY_ID}`])
  })
})

// ------------------------------------------------------------------------------------------------
// A quarantined reply is an unverified possible match that may be unrelated personal mail: its text
// is owner-only (views.inquiries.reply_content_visible). The list rows carry an availability claim
// DERIVED from that text; it is not shown to anyone else and is never shown as a stated fact.
// ------------------------------------------------------------------------------------------------

function quarantinedListApi(role: 'owner' | 'reviewer'): FakeApi {
  const quarantined = replySummary({
    reply_id: '77777777-7777-4777-8777-777777777778',
    quarantined: true,
    availability: 'sold',
    received_at: '2026-10-07T13:00:00Z',
  })
  return fakeApi({
    'GET /api/me': () => ok(me(role)),
    'GET /api/replies': () => ok({ items: [replySummary(), quarantined] }),
    'GET /api/inquiries/:id': () => ok(inquiry({ reply_count: 2 })),
  })
}

describe('quarantined replies in lists', () => {
  it('a reviewer never sees the availability derived from a quarantined (withheld) reply', async () => {
    renderApp('/replies', { api: quarantinedListApi('reviewer') })
    const rows = await screen.findAllByTestId('reply-row')
    expect(rows).toHaveLength(2)
    expect(within(rows[0]!).getByText('available')).toBeInTheDocument()
    expect(rows[1]).not.toHaveTextContent('sold')
    expect(within(rows[1]!).getByTestId('availability-withheld')).toHaveTextContent('withheld')
  })

  it('the same on the inquiry detail page', async () => {
    renderApp(`/inquiries/${INQUIRY_ID}`, { api: quarantinedListApi('reviewer') })
    const table = await screen.findByRole('table', { name: 'Replies' })
    const rows = within(table).getAllByTestId('reply-row')
    expect(rows[1]).not.toHaveTextContent('sold')
  })

  it('the owner sees it, labelled as an unverified match', async () => {
    renderApp('/replies', { api: quarantinedListApi('owner') })
    const rows = await screen.findAllByTestId('reply-row')
    expect(rows[1]).toHaveTextContent('sold (unverified match)')
  })
})

// ------------------------------------------------------------------------------------------------
// Payment-fraud safety: a quarantined reply is NOT verified to come from the seller (forwarded,
// changed address, ambiguous: anyone who saw the inquiry's Message-ID can write one). Its payment /
// reservation / identity-document requests must never be presented as "the seller asks".
// ------------------------------------------------------------------------------------------------

describe('escalations of a reply whose sender is not verified', () => {
  it('the owner sees the requests of a quarantined reply attributed to an UNVERIFIED sender, with a do-not-pay warning', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me('owner')),
      'GET /api/replies/:id': () =>
        ok(
          reply({
            quarantined: true,
            quarantine_reason: 'sender_changed',
            sender: { ...reply().sender, correlation_status: 'quarantined', matches_verified_recipient: false },
          }),
        ),
    })
    renderApp(`/replies/${REPLY_ID}`, { api })
    const escalations = await screen.findByTestId('escalations')
    expect(escalations).toHaveTextContent('not verified')
    expect(escalations).toHaveTextContent('Do not pay')
    expect(escalations).not.toHaveTextContent('the seller asks')
    expect(within(escalations).getAllByTestId('escalation')[0]).toHaveTextContent('payment: the sender asks for a payment or deposit.')
  })

  it('a matched reply from the verified seller address keeps the seller attribution', async () => {
    const api = fakeApi({
      'GET /api/me': () => ok(me('owner')),
      'GET /api/replies/:id': () => ok(reply()),
    })
    renderApp(`/replies/${REPLY_ID}`, { api })
    const escalations = await screen.findByTestId('escalations')
    expect(within(escalations).getAllByTestId('escalation')[0]).toHaveTextContent('payment: the seller asks for a payment or deposit.')
    expect(escalations).not.toHaveTextContent('not verified')
  })
})
