import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { apiError, deferred, fakeApi, ok } from './fakeApi'
import { FakeAuth } from './fakeAuth'
import { me, OTHER_WORKSPACE_ID, WORKSPACE_ID } from './fixtures'
import { renderApp } from './renderApp'

const OVERVIEW = {
  sources: [],
  running_sources: 0,
  paused_sources: 0,
  last_successful_scan_at: null,
  coverage_gaps: [],
  pending_reviews: { pending: 0, claimed: 0, needs_information: 0, watch: 0, shortlisted: 0, by_queue: [] },
  failed_deliveries: { uncertain: 0, blocked: 0, dead_letter: 0, retry_wait: 0 },
  activation_blockers: [],
  bridge_status: 'unavailable',
  coverage_note: 'SYNTHETIC coverage note.',
}

function baseApi() {
  return fakeApi({
    'GET /api/me': () => ok(me('reviewer')),
    'GET /api/overview': () => ok(OVERVIEW),
    'GET /api/outbox': () => ok({ items: [] }),
    'GET /api/candidates': () => ok({ items: [] }),
  })
}

describe('auth gating', () => {
  it('redirects a signed-out visitor to sign-in and never calls the API', async () => {
    const api = baseApi()
    const { router } = renderApp('/candidates?country=DE', { auth: new FakeAuth(false), api })
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/login')
    expect(router.state.location.search).toContain(encodeURIComponent('/candidates?country=DE'))
    expect(api.calls).toHaveLength(0)
  })

  it('shows the app for a signed-in member and sends the bearer token', async () => {
    const api = baseApi()
    renderApp('/', { api })
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    expect(api.calls[0]?.headers.get('Authorization')).toBe('Bearer synthetic-access-token-1')
  })

  it('signs in with a password and returns to the requested page', async () => {
    const auth = new FakeAuth(false)
    const api = baseApi()
    const { router } = renderApp('/login?next=%2Fcandidates', { auth, api })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'reviewer@e2e.invalid')
    await user.type(screen.getByLabelText('Password'), 'synthetic-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByRole('heading', { name: 'Candidate queue' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/candidates')
    expect(auth.signInWithPassword).toHaveBeenCalledWith({ email: 'reviewer@e2e.invalid', password: 'synthetic-password' })
  })

  it('refuses an open redirect in ?next=', async () => {
    const auth = new FakeAuth(false)
    const { router } = renderApp('/login?next=%2F%2Fevil.example%2F', { auth, api: baseApi() })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'reviewer@e2e.invalid')
    await user.type(screen.getByLabelText('Password'), 'synthetic-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await screen.findByRole('heading', { name: 'Overview' })
    expect(router.state.location.pathname).toBe('/')
  })

  it('shows a failed sign-in without revealing which part was wrong', async () => {
    const auth = new FakeAuth(false)
    auth.signInSucceeds = false
    renderApp('/login', { auth, api: baseApi() })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'nobody@e2e.invalid')
    await user.type(screen.getByLabelText('Password'), 'wrong')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    expect(await screen.findByText('Sign-in failed. Check the email address and password.')).toBeInTheDocument()
  })

  it('honours a cancelled login: a late session is dropped and the user stays signed out', async () => {
    const auth = new FakeAuth(false)
    const gate = deferred()
    auth.signInGate = gate.promise
    const { router } = renderApp('/login', { auth, api: baseApi() })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'reviewer@e2e.invalid')
    await user.type(screen.getByLabelText('Password'), 'synthetic-password')
    await user.click(screen.getByRole('button', { name: 'Sign in' }))
    await user.click(await screen.findByRole('button', { name: 'Cancel' }))
    expect(screen.getByText('Sign-in cancelled. You are not signed in.')).toBeInTheDocument()
    gate.release()
    await waitFor(() => expect(auth.signOut).toHaveBeenCalledWith({ scope: 'local' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in' })).toBeInTheDocument())
    expect(router.state.location.pathname).toBe('/login')
    expect(auth.session).toBeNull()
  })

  it('requests a magic link without creating accounts and with a same-origin callback', async () => {
    const auth = new FakeAuth(false)
    renderApp('/login', { auth, api: baseApi() })
    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('Email'), 'reviewer@e2e.invalid')
    await user.click(screen.getByRole('button', { name: 'Email me a sign-in link' }))
    expect(await screen.findByText(/a sign-in link is on its way/)).toBeInTheDocument()
    const call = auth.signInWithOtp.mock.calls[0]?.[0]
    expect(call?.options?.shouldCreateUser).toBe(false)
    expect(call?.options?.emailRedirectTo).toMatch(new RegExp(`^${window.location.origin}/auth/callback`))
  })

  it('shows a cancelled or failed magic-link callback as inert text', async () => {
    renderApp('/auth/callback?error=access_denied&error_description=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E', {
      auth: new FakeAuth(false),
      api: baseApi(),
    })
    expect(await screen.findByRole('heading', { name: 'Sign-in not completed' })).toBeInTheDocument()
    expect(screen.getByText('<img src=x onerror=alert(1)>')).toBeInTheDocument()
    expect(document.querySelector('img')).toBeNull()
  })

  it('signs out and returns to the sign-in page', async () => {
    const auth = new FakeAuth(true)
    renderApp('/', { auth, api: baseApi() })
    const user = userEvent.setup()
    await screen.findByRole('heading', { name: 'Overview' })
    await user.click(screen.getAllByRole('button', { name: 'Sign out' })[0]!)
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument()
    expect(auth.signOut).toHaveBeenCalled()
  })

  it('returns to sign-in with a notice when the backend keeps refusing the session', async () => {
    const auth = new FakeAuth(true)
    auth.refreshFails = true
    const api = fakeApi({ 'GET /api/me': () => apiError(401, 'UNAUTHENTICATED', 'The access token is invalid or expired') })
    renderApp('/', { auth, api })
    expect(await screen.findByText('Your session expired or was revoked. Please sign in again.')).toBeInTheDocument()
    expect(auth.refreshSession).toHaveBeenCalledTimes(1)
  })

  it('asks for a workspace when the user has several memberships and then sends X-Workspace-Id', async () => {
    const api = fakeApi({
      'GET /api/me': (call) => ok(me('reviewer', { memberships: 2, workspaceId: call.headers.get('X-Workspace-Id') ?? WORKSPACE_ID })),
      'GET /api/overview': () => ok(OVERVIEW),
      'GET /api/outbox': () => ok({ items: [] }),
    })
    renderApp('/', { api })
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: /SYNTHETIC workspace B/ }))
    expect(await screen.findByRole('heading', { name: 'Overview' })).toBeInTheDocument()
    expect(screen.getByTestId('workspace-name')).toHaveTextContent('SYNTHETIC workspace B')
    const overviewCall = api.callsTo('GET /api/overview')[0]
    expect(overviewCall?.headers.get('X-Workspace-Id')).toBe(OTHER_WORKSPACE_ID)
    expect(api.callsTo('GET /api/me')[0]?.headers.get('X-Workspace-Id')).toBeNull()
  })

  it('explains a missing membership', async () => {
    const api = fakeApi({ 'GET /api/me': () => apiError(403, 'FORBIDDEN', 'No active membership for the requested workspace') })
    renderApp('/', { api })
    expect(await screen.findByRole('heading', { name: 'Workspace unavailable' })).toBeInTheDocument()
    expect(screen.getByText(/no active membership/)).toBeInTheDocument()
    expect(screen.getByTestId('correlation-id')).toHaveTextContent('req-synthetic-error-403')
  })
})
