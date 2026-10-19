/** A scriptable stand-in for supabase-js `auth` (SYNTHETIC tokens; no network). */
import { vi } from 'vitest'
import type { AuthChangeEvent, AuthClient, Session } from '../auth/authClient'

type Listener = (event: AuthChangeEvent, session: Session | null) => void

export const TEST_USER_ID = '11111111-1111-4111-8111-111111111111'

export class FakeAuth implements AuthClient {
  session: Session | null
  counter = 0
  refreshFails = false
  private listeners = new Set<Listener>()
  /** When set, `signInWithPassword` waits for this promise before answering. */
  signInGate: Promise<void> | null = null
  signInSucceeds = true

  constructor(signedIn = true) {
    this.session = signedIn ? this.makeSession() : null
  }

  makeSession(): Session {
    this.counter += 1
    return {
      access_token: `synthetic-access-token-${this.counter}`,
      refresh_token: `synthetic-refresh-token-${this.counter}`,
      token_type: 'bearer',
      expires_in: 3600,
      expires_at: Math.floor(Date.now() / 1000) + 3600,
      user: { id: TEST_USER_ID, email: 'reviewer@e2e.invalid' },
    } as unknown as Session
  }

  emit(event: AuthChangeEvent): void {
    // Copy first: a listener may unsubscribe while being notified.
    const listeners = Array.from(this.listeners)
    for (const listener of listeners) listener(event, this.session)
  }

  getSession = vi.fn(async () => ({ data: { session: this.session }, error: null }))

  refreshSession = vi.fn(async () => {
    if (this.refreshFails || !this.session) {
      return { data: { session: null }, error: { message: 'Invalid Refresh Token', status: 400 } }
    }
    this.session = this.makeSession()
    this.emit('TOKEN_REFRESHED')
    return { data: { session: this.session }, error: null }
  })

  signInWithPassword = vi.fn(async (_credentials: { email: string; password: string }) => {
    if (this.signInGate) await this.signInGate
    if (!this.signInSucceeds) {
      return { data: { session: null }, error: { message: 'Invalid login credentials', status: 400 } }
    }
    this.session = this.makeSession()
    this.emit('SIGNED_IN')
    return { data: { session: this.session }, error: null }
  })

  signInWithOtp = vi.fn(async (_credentials: { email: string; options?: { emailRedirectTo?: string; shouldCreateUser?: boolean } }) => ({
    error: null,
  }))

  signOut = vi.fn(async (_options?: { scope?: 'global' | 'local' | 'others' }) => {
    this.session = null
    this.emit('SIGNED_OUT')
    return { error: null }
  })

  onAuthStateChange(callback: Listener) {
    this.listeners.add(callback)
    queueMicrotask(() => callback('INITIAL_SESSION', this.session))
    return { data: { subscription: { unsubscribe: () => this.listeners.delete(callback) } } }
  }
}
