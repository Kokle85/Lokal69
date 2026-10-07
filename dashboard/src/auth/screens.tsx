import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router'
import { LoadingState } from '../components/ui'
import { safeNextPath } from '../format'
import { useAuth } from './AuthProvider'

/** Protects the app routes: only a signed-in session renders them. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const { status } = useAuth()
  const location = useLocation()
  if (status === 'loading') return <LoadingState label="Checking your session" />
  if (status !== 'signed_in') {
    const next = encodeURIComponent(`${location.pathname}${location.search}`)
    return <Navigate to={`/login?next=${next}`} replace />
  }
  return <>{children}</>
}

type LoginMessage = { tone: 'info' | 'error' | 'ok'; text: string } | null

export function LoginScreen() {
  const { status, auth, notice, setNotice } = useAuth()
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const next = safeNextPath(params.get('next'))
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState<'password' | 'link' | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const [message, setMessage] = useState<LoginMessage>(null)
  const attempt = useRef(0)

  useEffect(() => {
    if (status === 'signed_in' && !cancelling) {
      setNotice(null)
      navigate(next, { replace: true })
    }
  }, [status, cancelling, next, navigate, setNotice])

  async function signInWithPassword(event: FormEvent) {
    event.preventDefault()
    if (busy || cancelling) return
    const mine = ++attempt.current
    setBusy('password')
    setMessage({ tone: 'info', text: 'Signing in…' })
    try {
      const { data, error } = await auth.signInWithPassword({ email: email.trim(), password })
      if (mine !== attempt.current) {
        // Cancelled while in flight: honour the cancel and drop the session it may have created.
        if (data.session) await auth.signOut({ scope: 'local' })
        return
      }
      if (error || !data.session) {
        setMessage({ tone: 'error', text: 'Sign-in failed. Check the email address and password.' })
        return
      }
      setMessage({ tone: 'ok', text: 'Signed in.' })
    } catch {
      if (mine === attempt.current) {
        setMessage({ tone: 'error', text: 'Sign-in failed: the authentication service could not be reached.' })
      }
    } finally {
      // The form stays locked until the (possibly cancelled) request has settled, so a late
      // answer can never sign out a newer session.
      setBusy(null)
      setCancelling(false)
      setPassword('')
    }
  }

  function cancel() {
    attempt.current += 1
    setCancelling(true)
    setMessage({ tone: 'info', text: 'Sign-in cancelled. You are not signed in.' })
  }

  async function sendMagicLink() {
    if (busy || cancelling) return
    const address = email.trim()
    if (!address) {
      setMessage({ tone: 'error', text: 'Enter your email address first.' })
      return
    }
    const mine = ++attempt.current
    setBusy('link')
    try {
      const redirect = new URL('/auth/callback', window.location.origin)
      redirect.searchParams.set('next', next)
      const { error } = await auth.signInWithOtp({
        email: address,
        options: { emailRedirectTo: redirect.toString(), shouldCreateUser: false },
      })
      if (mine !== attempt.current) return
      // The same wording whether or not the address has an account (no account enumeration).
      setMessage(
        error && error.status !== undefined && error.status >= 500
          ? { tone: 'error', text: 'The sign-in link could not be sent right now. Try again later.' }
          : {
              tone: 'ok',
              text: 'If this address belongs to a dashboard user, a sign-in link is on its way. Open it in this browser.',
            },
      )
    } catch {
      if (mine === attempt.current) setMessage({ tone: 'error', text: 'The authentication service could not be reached.' })
    } finally {
      if (mine === attempt.current) setBusy(null)
    }
  }

  return (
    <main className="centered-page login" id="main" tabIndex={-1}>
      <h1>Sign in</h1>
      <p className="muted">Private review dashboard. Access requires an active workspace membership.</p>
      {notice ? (
        <p className="notice notice-warn" role="status">
          {notice}
        </p>
      ) : null}
      <form onSubmit={(event) => void signInWithPassword(event)} className="form" aria-describedby="login-status">
        <label htmlFor="login-email">Email</label>
        <input
          id="login-email"
          type="email"
          autoComplete="username"
          required
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          disabled={busy !== null}
        />
        <label htmlFor="login-password">Password</label>
        <input
          id="login-password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={busy !== null}
        />
        <div className="form-actions">
          <button type="submit" className="button" disabled={busy !== null || !password}>
            {busy === 'password' ? 'Signing in…' : 'Sign in'}
          </button>
          {busy === 'password' ? (
            <button type="button" className="button secondary" onClick={cancel} disabled={cancelling}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
          ) : null}
          <button type="button" className="button secondary" onClick={() => void sendMagicLink()} disabled={busy !== null}>
            {busy === 'link' ? 'Sending link…' : 'Email me a sign-in link'}
          </button>
        </div>
      </form>
      <p id="login-status" role="status" aria-live="polite" className={message ? `login-message ${message.tone}` : 'login-message'}>
        {message?.text ?? ''}
      </p>
    </main>
  )
}

function urlErrorParams(location: { search: string; hash: string }): { error: string; description: string | null } | null {
  const sources = [new URLSearchParams(location.search), new URLSearchParams(location.hash.replace(/^#/, ''))]
  for (const params of sources) {
    const error = params.get('error') ?? params.get('error_code')
    if (error) return { error, description: params.get('error_description') }
  }
  return null
}

/**
 * Landing page of the magic link (PKCE: `?code=` is exchanged by supabase-js itself). A cancelled
 * or failed sign-in (`?error=...`) is shown as plain text and the URL is cleaned.
 */
export function AuthCallbackScreen() {
  const { status } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [failure] = useState(() => urlErrorParams(location))
  const next = safeNextPath(new URLSearchParams(location.search).get('next'))

  useEffect(() => {
    // Never keep auth codes or error details in the address bar / history.
    window.history.replaceState(null, '', '/auth/callback')
  }, [])

  useEffect(() => {
    if (!failure && status === 'signed_in') navigate(next, { replace: true })
  }, [failure, status, next, navigate])

  if (failure) {
    return (
      <main className="centered-page" id="main" tabIndex={-1}>
        <h1>Sign-in not completed</h1>
        <p role="alert">
          The sign-in was cancelled or the link is invalid or expired. You are not signed in.
          {failure.description ? (
            <>
              {' '}
              Details: <span className="untrusted">{failure.description}</span>
            </>
          ) : null}
        </p>
        <p>
          <Link to="/login">Back to sign in</Link>
        </p>
      </main>
    )
  }
  if (status === 'loading' || status === 'signed_in') return <LoadingState label="Completing sign-in" />
  return (
    <main className="centered-page" id="main" tabIndex={-1}>
      <h1>Sign-in not completed</h1>
      <p role="alert">The sign-in link is invalid, expired or was opened in another browser. You are not signed in.</p>
      <p>
        <Link to="/login">Back to sign in</Link>
      </p>
    </main>
  )
}
