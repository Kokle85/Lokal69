import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { ReviewCaseView, Role } from '../api/types'
import { writePendingSubmission } from '../review/pendingMarker'
import { apiError, deferred, fakeApi, ok, type FakeApi } from './fakeApi'
import { TEST_USER_ID } from './fakeAuth'
import { candidateDetail, CASE_ID, CLAIM_TOKEN, claimResult, decision, LISTING_ID, me, reviewCase } from './fixtures'
import { renderApp } from './renderApp'

interface World {
  api: FakeApi
  current: { value: ReviewCaseView }
}

/** A small stateful backend for one case: claim -> v2, submit -> v3 (watch). */
function reviewWorld(role: Role = 'reviewer'): World {
  const current = { value: reviewCase() }
  const api = fakeApi({
    'GET /api/me': () => ok(me(role)),
    'GET /api/reviews/:id': () => ok(current.value),
    'GET /api/candidates/:id': () => ok(candidateDetail()),
    'POST /api/reviews/:id/claim': () => {
      current.value = { ...current.value, case_version: 2, state: 'claimed', claim: { claimed: true, held_by_caller: true, expires_at: claimResult().expires_at } }
      return ok(claimResult())
    },
    'POST /api/reviews/:id/submit': (call) => {
      const body = call.body as { outcome: 'watch' }
      const saved = decision({ outcome: body.outcome })
      current.value = { ...current.value, case_version: 3, state: 'watch', claim: { claimed: false, held_by_caller: false, expires_at: null }, decisions: [saved], latest_decision_id: saved.decision_id }
      return ok(saved, 201)
    },
  })
  return { api, current }
}

async function claimAndFill(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: 'Claim' }))
  await screen.findByText(/You hold the claim until/)
  await user.click(screen.getByRole('radio', { name: /Watch/ }))
  await user.click(screen.getByRole('checkbox', { name: 'price in band' }))
  await user.type(screen.getByLabelText(/^Summary/), 'SYNTHETIC: price in band; watch for a drop.')
}

describe('review submission', () => {
  it('sends ONE request for a double-clicked submit and claims saved only after the server confirms', async () => {
    const world = reviewWorld()
    const gate = deferred()
    const original = world.api
    original.on('POST /api/reviews/:id/submit', async (call) => {
      await gate.promise
      const saved = decision()
      world.current.value = { ...world.current.value, case_version: 3, state: 'watch', decisions: [saved] }
      expect(call.body).toMatchObject({ claim_token: CLAIM_TOKEN, expected_version: 2, listing_revision: 2, outcome: 'watch' })
      return ok(saved, 201)
    })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await claimAndFill(user)
    const submit = screen.getByRole('button', { name: 'Submit decision' })
    // Two clicks in the same tick: the second must not send anything.
    act(() => {
      fireEvent.click(submit)
      fireEvent.click(submit)
    })
    await user.click(submit)
    expect(await screen.findByTestId('mutation-pending')).toHaveTextContent('Not saved yet')
    expect(screen.queryByTestId('decision-saved')).toBeNull()
    expect(world.api.callsTo('POST /api/reviews/:id/submit')).toHaveLength(1)
    gate.release()
    expect(await screen.findByTestId('decision-saved')).toHaveTextContent('Decision saved: watch')
    expect(world.api.callsTo('POST /api/reviews/:id/submit')).toHaveLength(1)
    // The (now disabled) submit button does not swallow keyboard focus: it moves to the result.
    await waitFor(() => expect(screen.getByTestId('decision-saved').closest('.mutation-status')).toHaveFocus())
    await waitFor(() => expect(screen.getAllByTestId('decision-entry')).toHaveLength(1))
  })

  it('shows a lost response as not confirmed and resolves it with an identical same-key retry', async () => {
    const world = reviewWorld()
    let attempts = 0
    world.api.on('POST /api/reviews/:id/submit', (call) => {
      attempts += 1
      const saved = decision()
      world.current.value = { ...world.current.value, case_version: 3, state: 'watch', decisions: [saved] }
      if (attempts === 1) throw new TypeError('network connection lost') // committed, but the answer never arrived
      expect(call.body).toBeDefined()
      return ok(saved, 201)
    })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await claimAndFill(user)
    await user.click(screen.getByRole('button', { name: 'Submit decision' }))
    const unconfirmed = await screen.findByTestId('mutation-unconfirmed')
    expect(unconfirmed).toHaveTextContent('Not confirmed: it may or may not have been saved')
    expect(screen.queryByTestId('decision-saved')).toBeNull()
    // The form is locked while the outcome is unknown: no second write with a new key is possible.
    expect(screen.getByRole('button', { name: 'Submit decision' })).toBeDisabled()
    expect(sessionStorage.getItem(`suvdash:pending-submit:${CASE_ID}`)).toContain('review-submit:')
    expect(sessionStorage.getItem(`suvdash:pending-submit:${CASE_ID}`)).not.toContain(CLAIM_TOKEN)
    await user.click(within(unconfirmed).getByRole('button', { name: 'Retry the same request' }))
    expect(await screen.findByTestId('decision-saved')).toBeInTheDocument()
    expect(screen.getByTestId('decision-saved').closest('[data-testid="mutation-confirmed"]')).toHaveTextContent(
      'confirmed by the server on an idempotent retry',
    )
    const [first, second] = world.api.callsTo('POST /api/reviews/:id/submit')
    expect(first?.rawBody).toBe(second?.rawBody)
    expect(first?.headers.get('Idempotency-Key')).toBe(second?.headers.get('Idempotency-Key'))
    expect(sessionStorage.getItem(`suvdash:pending-submit:${CASE_ID}`)).toBeNull()
  })

  it('keeps an unconfirmed note unconfirmed when the retry is refused before evaluation (429), so no second key can duplicate it', async () => {
    let attempts = 0
    const api = fakeApi({
      'GET /api/me': () => ok(me('reviewer')),
      'GET /api/candidates/:id': () => ok(candidateDetail()),
      'POST /api/listings/:id/notes': (call) => {
        attempts += 1
        if (attempts === 1) throw new TypeError('network connection lost') // committed; the answer never arrived
        if (attempts === 2) {
          return apiError(429, 'RATE_LIMITED', 'Too many requests', { retryable: true, correlation: 'req-rate-limited-1' })
        }
        const body = call.body as { note: string }
        return ok({ note_id: '99999999-9999-4999-8999-999999999999', listing_id: LISTING_ID, label: 'reviewer', body: body.note, created_at: '2026-10-07T10:00:00Z', replayed: true }, 201)
      },
    })
    renderApp(`/candidates/${LISTING_ID}`, { api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Add a private note'), 'SYNTHETIC note sent over a flaky connection.')
    await user.click(screen.getByRole('button', { name: 'Add note' }))
    let unconfirmed = await screen.findByTestId('mutation-unconfirmed')
    await user.click(within(unconfirmed).getByRole('button', { name: 'Retry the same request' }))
    // The 429 refused the RETRY before the server looked at it: the first send is still unknown.
    unconfirmed = await screen.findByTestId('mutation-unconfirmed')
    expect(unconfirmed).toHaveTextContent('refused before the server looked at it')
    expect(screen.queryByTestId('mutation-rejected')).toBeNull()
    expect(screen.getByRole('button', { name: 'Add note' })).toBeDisabled()
    await user.click(within(unconfirmed).getByRole('button', { name: 'Retry the same request' }))
    expect(await screen.findByTestId('mutation-confirmed')).toHaveTextContent('Note saved.')
    const sends = api.callsTo('POST /api/listings/:id/notes')
    expect(sends).toHaveLength(3)
    expect(new Set(sends.map((call) => call.headers.get('Idempotency-Key'))).size).toBe(1)
    expect(new Set(sends.map((call) => call.rawBody)).size).toBe(1)
  })

  it('shows ALREADY_CLAIMED with a reload path and the correlation id', async () => {
    const world = reviewWorld()
    world.current.value = reviewCase({ claim: { claimed: true, held_by_caller: false, expires_at: new Date(Date.now() + 60_000).toISOString() } })
    world.api.on('POST /api/reviews/:id/claim', () =>
      apiError(409, 'ALREADY_CLAIMED', 'Another reviewer holds an active claim', { correlation: 'req-already-claimed-1' }),
    )
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    expect(await screen.findByTestId('claim-state')).toHaveTextContent('Claimed by another reviewer')
    await user.click(screen.getByRole('button', { name: 'Claim' }))
    expect(await screen.findByText('Already claimed by another reviewer')).toBeInTheDocument()
    expect(screen.getByTestId('correlation-id')).toHaveTextContent('req-already-claimed-1')
    const reloads = world.api.callsTo('GET /api/reviews/:id').length
    await user.click(screen.getByRole('button', { name: 'Reload the case' }))
    await waitFor(() => expect(world.api.callsTo('GET /api/reviews/:id').length).toBe(reloads + 1))
  })

  it('shows CLAIM_EXPIRED on submit, keeps the draft and forgets the stale handle', async () => {
    const world = reviewWorld()
    world.api.on('POST /api/reviews/:id/submit', () => apiError(409, 'CLAIM_EXPIRED', 'The claim token is not the current handle for this case'))
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await claimAndFill(user)
    await user.click(screen.getByRole('button', { name: 'Submit decision' }))
    expect(await screen.findByText('Your claim expired or is no longer current')).toBeInTheDocument()
    expect(screen.getByLabelText(/^Summary/)).toHaveValue('SYNTHETIC: price in band; watch for a drop.')
    expect(screen.queryByTestId('decision-saved')).toBeNull()
    expect(screen.getByRole('button', { name: 'Submit decision' })).toBeDisabled()
    // The claim's earlier "Claimed until ..." confirmation is gone with the stale handle.
    expect(screen.queryByText(/Claimed until/)).toBeNull()
    expect(screen.getByTestId('claim-state')).not.toHaveTextContent('You hold the claim until')
  })

  it('shows VERSION_CONFLICT for a newer listing revision with a reload path', async () => {
    const world = reviewWorld()
    world.api.on('POST /api/reviews/:id/submit', () =>
      apiError(409, 'VERSION_CONFLICT', 'The listing has a newer revision; reload before deciding', {
        details: { expected_listing_revision: 2, current_listing_revision: 3 },
      }),
    )
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await claimAndFill(user)
    await user.click(screen.getByRole('button', { name: 'Submit decision' }))
    expect(await screen.findByText('The case or listing changed')).toBeInTheDocument()
    expect(screen.getByText(/listing revision 3 is now current/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reload the case' })).toBeInTheDocument()
  })

  it('prefills "needs inspection" as a needs_information decision listing the open checklist items', async () => {
    const world = reviewWorld()
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Claim' }))
    await screen.findByText(/You hold the claim until/)
    await waitFor(() => expect(world.api.callsTo('GET /api/candidates/:id').length).toBeGreaterThan(0))
    await user.click(screen.getByRole('button', { name: 'Needs inspection' }))
    expect(screen.getByRole('radio', { name: /Needs information/ })).toBeChecked()
    expect(screen.getByLabelText(/^Missing information/)).toHaveValue('What does the current inspection cover?')
    await user.click(screen.getByRole('button', { name: 'Submit decision' }))
    await screen.findByTestId('decision-saved')
    const body = world.api.callsTo('POST /api/reviews/:id/submit')[0]?.body as Record<string, unknown>
    expect(body.outcome).toBe('needs_information')
    expect(body.reason_codes).toEqual(['needs_inspection'])
    expect(body.missing_information).toEqual(['What does the current inspection cover?'])
  })

  it('blocks action reason codes on other outcomes and requires reasons and a summary', async () => {
    const world = reviewWorld()
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Claim' }))
    await screen.findByText(/You hold the claim until/)
    await user.click(screen.getByRole('radio', { name: /Reject/ }))
    expect(screen.getByRole('checkbox', { name: 'needs inspection' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Submit decision' }))
    expect(await screen.findByText('Give at least one reason code.')).toBeInTheDocument()
    expect(screen.getByText('The summary needs at least 10 characters.')).toBeInTheDocument()
    expect(world.api.callsTo('POST /api/reviews/:id/submit')).toHaveLength(0)
  })

  it('rotates the claim handle when a claim replay comes back without a token', async () => {
    const world = reviewWorld()
    let claims = 0
    world.api.on('POST /api/reviews/:id/claim', () => {
      claims += 1
      return ok(claims === 1 ? claimResult({ claim_token: null, claim_token_redacted: true }) : claimResult({ case_version: 3, rotated: true }))
    })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Claim' }))
    expect(await screen.findByText(/You hold the claim until/)).toBeInTheDocument()
    const [first, second] = world.api.callsTo('POST /api/reviews/:id/claim')
    expect((second!.body as { expected_version: number }).expected_version).toBe(2)
    expect(first?.headers.get('Idempotency-Key')).not.toBe(second?.headers.get('Idempotency-Key'))
  })

  it('hides every mutation control from a viewer', async () => {
    const world = reviewWorld('viewer')
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    expect(await screen.findByText(/Read-only: your role \(viewer\)/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Claim' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Submit decision' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Needs inspection' })).toBeNull()
  })

  it('after a reload with a pending action, reports the server state and resends nothing', async () => {
    const world = reviewWorld()
    const saved = decision({ case_version: 2 })
    world.current.value = reviewCase({ case_version: 3, state: 'watch', decisions: [saved], latest_decision_id: saved.decision_id })
    writePendingSubmission({
      caseId: CASE_ID,
      userId: TEST_USER_ID,
      idempotencyKey: 'review-submit:k-earlier-attempt',
      outcome: 'watch',
      expectedVersion: 2,
      startedAt: new Date().toISOString(),
    })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    expect(await screen.findByTestId('earlier-submission-recorded')).toHaveTextContent('The server recorded it: watch')
    expect(screen.getByTestId('earlier-submission')).toHaveTextContent('Nothing was resent automatically.')
    expect(world.api.callsTo('POST /api/reviews/:id/submit')).toHaveLength(0)
    await waitFor(() => expect(sessionStorage.getItem(`suvdash:pending-submit:${CASE_ID}`)).toBeNull())
  })

  it('after a reload, never mistakes another reviewer\'s decision on the same version for ours (decided_by_caller)', async () => {
    const world = reviewWorld()
    const theirs = decision({ case_version: 2, decided_by_caller: false, actor: { principal_id: '22222222-2222-4222-8222-222222222222', principal_kind: 'user', role: 'reviewer' } })
    world.current.value = reviewCase({ case_version: 3, state: 'watch', decisions: [theirs], latest_decision_id: theirs.decision_id })
    writePendingSubmission({
      caseId: CASE_ID,
      userId: TEST_USER_ID,
      idempotencyKey: 'review-submit:k-earlier-attempt',
      outcome: 'watch',
      expectedVersion: 2,
      startedAt: new Date().toISOString(),
    })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    expect(await screen.findByTestId('earlier-submission-other')).toHaveTextContent('Your submission was not recorded: another reviewer recorded a decision')
    expect(screen.queryByTestId('earlier-submission-recorded')).toBeNull()
    expect(within(screen.getByTestId('decision-history')).getByText(/by a reviewer/)).toBeInTheDocument()
    expect(world.api.callsTo('POST /api/reviews/:id/submit')).toHaveLength(0)
    await waitFor(() => expect(sessionStorage.getItem(`suvdash:pending-submit:${CASE_ID}`)).toBeNull())
  })

  it('marks the caller\'s own decisions as "by you" in the history', async () => {
    const world = reviewWorld()
    const mine = decision({ case_version: 2 })
    world.current.value = reviewCase({ case_version: 3, state: 'watch', decisions: [mine], latest_decision_id: mine.decision_id })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    expect(await screen.findByTestId('decision-entry')).toHaveTextContent('by you')
  })

  it('after a reload with an unrecorded pending action, asks to claim again', async () => {
    const world = reviewWorld()
    world.current.value = reviewCase({ case_version: 2, state: 'claimed', claim: { claimed: true, held_by_caller: true, expires_at: new Date(Date.now() + 60_000).toISOString() } })
    writePendingSubmission({
      caseId: CASE_ID,
      userId: TEST_USER_ID,
      idempotencyKey: 'review-submit:k-earlier-attempt',
      outcome: 'watch',
      expectedVersion: 2,
      startedAt: new Date().toISOString(),
    })
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    expect(await screen.findByTestId('earlier-submission-not-recorded')).toHaveTextContent('claim the case again')
    expect(screen.getByTestId('claim-state')).toHaveTextContent('handle is not available here')
    expect(screen.getByRole('button', { name: 'Claim again' })).toBeInTheDocument()
    expect(world.api.callsTo('POST /api/reviews/:id/submit')).toHaveLength(0)
  })

  it('warns when the claim expires while editing and keeps the draft for a re-claim', async () => {
    const world = reviewWorld()
    world.api.on('POST /api/reviews/:id/claim', () => ok(claimResult({ expires_at: new Date(Date.now() - 1_000).toISOString() })))
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Claim' }))
    expect(await screen.findByText(/Your claim expired at/)).toBeInTheDocument()
    await user.type(screen.getByLabelText(/^Summary/), 'SYNTHETIC draft written while the claim lapsed.')
    expect(screen.getByRole('button', { name: 'Submit decision' })).toBeDisabled()
    expect(screen.getByText('Claim the case to submit.')).toBeInTheDocument()
    world.api.on('POST /api/reviews/:id/claim', () => ok(claimResult({ case_version: 3, rotated: true })))
    await user.click(screen.getByRole('button', { name: /Claim/ }))
    expect(await screen.findByText(/You hold the claim until/)).toBeInTheDocument()
    expect(screen.getByLabelText(/^Summary/)).toHaveValue('SYNTHETIC draft written while the claim lapsed.')
    expect(screen.getByRole('button', { name: 'Submit decision' })).toBeEnabled()
  })

  it('does not carry a draft or a mutation state over to another case (back/forward between cases)', async () => {
    const world = reviewWorld()
    const { router } = renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    const user = userEvent.setup()
    await claimAndFill(user)
    const other = '00000000-0000-4000-8000-0000000000c2'
    await act(async () => {
      await router.navigate(`/reviews/${other}`)
    })
    await waitFor(() => expect(world.api.calls.some((call) => call.path === `/api/reviews/${other}`)).toBe(true))
    expect(await screen.findByLabelText(/^Summary/)).toHaveValue('')
    expect(screen.queryByText(/Claimed until/)).toBeNull()
  })

  it('links the candidate of the case', async () => {
    const world = reviewWorld()
    renderApp(`/reviews/${CASE_ID}`, { api: world.api })
    expect(await screen.findByRole('link', { name: 'Candidate detail' })).toHaveAttribute('href', `/candidates/${LISTING_ID}`)
  })
})
