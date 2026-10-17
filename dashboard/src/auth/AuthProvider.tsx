import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import type { TokenSource } from '../api/client'
import type { AuthClient } from './authClient'

export type AuthStatus = 'loading' | 'signed_in' | 'signed_out'

export interface AuthContextValue {
  status: AuthStatus
  /** Identity for display and for keying per-user UI state; authorization is the backend's job. */
  userId: string | null
  email: string | null
  /** A one-off message for the sign-in screen (e.g. "your session expired"). */
  notice: string | null
  setNotice(notice: string | null): void
  auth: AuthClient
  tokens: TokenSource
  signOut(): Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

interface Identity {
  status: AuthStatus
  userId: string | null
  email: string | null
}

export function AuthProvider({ auth, children }: { auth: AuthClient; children: ReactNode }) {
  const [identity, setIdentity] = useState<Identity>({ status: 'loading', userId: null, email: null })
  const [notice, setNotice] = useState<string | null>(null)
  const signingOut = useRef(false)

  useEffect(() => {
    // The callback is deliberately SYNCHRONOUS: supabase-js deprecates async callbacks (a nested
    // auth call inside one can deadlock). Only identity fields are kept; never the tokens.
    const { data } = auth.onAuthStateChange((_event, session) => {
      const user = session?.user ?? null
      setIdentity((previous) => {
        const next: Identity = user
          ? { status: 'signed_in', userId: user.id, email: user.email ?? null }
          : { status: 'signed_out', userId: null, email: null }
        return previous.status === next.status && previous.userId === next.userId && previous.email === next.email
          ? previous
          : next
      })
    })
    return () => data.subscription.unsubscribe()
  }, [auth])

  const signOut = useCallback(async () => {
    signingOut.current = true
    try {
      const { error } = await auth.signOut()
      if (error) {
        // The server-side revocation failed (offline?): still drop the local session.
        await auth.signOut({ scope: 'local' })
      }
    } finally {
      signingOut.current = false
    }
  }, [auth])

  const tokens = useMemo<TokenSource>(
    () => ({
      async getAccessToken() {
        const { data } = await auth.getSession()
        return data.session?.access_token ?? null
      },
      async refreshAccessToken() {
        const { data, error } = await auth.refreshSession()
        if (error) return null
        return data.session?.access_token ?? null
      },
      onUnauthenticated() {
        if (signingOut.current) return
        setNotice('Your session expired or was revoked. Please sign in again.')
        void auth.signOut({ scope: 'local' })
      },
    }),
    [auth],
  )

  const value = useMemo<AuthContextValue>(
    () => ({ ...identity, notice, setNotice, auth, tokens, signOut }),
    [identity, notice, auth, tokens, signOut],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}
