/**
 * Work package D2 (dashboard), F3/OPS-04: no real seller inquiry is reserved before the owner's
 * activation canary of the configured sender's CURRENT version is complete (a correlated test
 * reply; the server refuses with `activation_canary_incomplete`). The control screen names that
 * gate and never claims automatic inquiries are possible without it.
 */
import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { sendingReadinessProblems } from '../screens/inquiries/shared'
import { fakeApi, ok, type FakeApi, type Handler } from './fakeApi'
import { me } from './fixtures'
import { renderApp } from './renderApp'
import { control } from './v11Fixtures'

function api(routes: Record<string, Handler>): FakeApi {
  return fakeApi({ 'GET /api/me': () => ok(me('reviewer')), ...routes })
}

describe('D2 F3/OPS-04: the activation-canary gate is named, never claimed open', () => {
  it('an incomplete canary is a readiness problem with its refusal code', () => {
    const problems = sendingReadinessProblems(control({ activation_canary_complete: false, automatic_inquiries_possible: false }))
    expect(problems).toHaveLength(1)
    expect(problems[0]).toContain('activation canary')
    expect(problems[0]).toContain('activation_canary_incomplete')
    expect(sendingReadinessProblems(control())).toEqual([])
  })

  it('the control screen says nothing can be sent and shows the canary state', async () => {
    const view = control({ activation_canary_complete: false, automatic_inquiries_possible: false })
    renderApp('/inquiry-control', { api: api({ 'GET /api/inquiry-control': () => ok(view) }) })
    const state = await screen.findByTestId('sending-state')
    expect(state).toHaveTextContent('nothing can be sent now')
    expect(state).toHaveTextContent('activation_canary_incomplete')
    expect(state).not.toHaveTextContent('the server does not report automatic inquiries as possible')
    expect(screen.getByTestId('activation-canary-gate')).toHaveAttribute('data-complete', 'no')
    expect(screen.getByTestId('automatic-possible')).toHaveAttribute('data-possible', 'no')
  })

  it('a complete canary is shown as complete', async () => {
    renderApp('/inquiry-control', { api: api({ 'GET /api/inquiry-control': () => ok(control()) }) })
    expect(await screen.findByTestId('activation-canary-gate')).toHaveAttribute('data-complete', 'yes')
    expect(screen.getByTestId('automatic-possible')).toHaveAttribute('data-possible', 'yes')
  })
})
