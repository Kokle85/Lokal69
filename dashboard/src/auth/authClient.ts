/**
 * The subset of supabase-js `auth` the dashboard uses, so tests can inject a fake.
 *
 * Sessions live ONLY in supabase-js' own storage (`persistSession`); the dashboard never copies an
 * access or refresh token into its own state, storage, URL or logs. Tokens are read with
 * `getSession()` at the moment a request is sent.
 */
import { createClient, type AuthChangeEvent, type Session, type SupabaseClient } from '@supabase/supabase-js'
import type { DashboardConfig } from '../config'

export type { AuthChangeEvent, Session }

export interface AuthError {
  message: string
  code?: string | undefined
  status?: number | undefined
}

export interface AuthClient {
  getSession(): Promise<{ data: { session: Session | null }; error: AuthError | null }>
  refreshSession(): Promise<{ data: { session: Session | null }; error: AuthError | null }>
  signInWithPassword(credentials: {
    email: string
    password: string
  }): Promise<{ data: { session: Session | null }; error: AuthError | null }>
  signInWithOtp(credentials: {
    email: string
    options?: { emailRedirectTo?: string; shouldCreateUser?: boolean }
  }): Promise<{ error: AuthError | null }>
  signOut(options?: { scope?: 'global' | 'local' | 'others' }): Promise<{ error: AuthError | null }>
  onAuthStateChange(callback: (event: AuthChangeEvent, session: Session | null) => void): {
    data: { subscription: { unsubscribe(): void } }
  }
}

let client: SupabaseClient | null = null

/** The single supabase-js client (Auth only; PKCE so tokens never travel in URLs). */
export function supabaseAuth(config: DashboardConfig): AuthClient {
  client ??= createClient(config.supabaseUrl, config.publishableKey, {
    auth: {
      persistSession: true,
      autoRefreshToken: true,
      detectSessionInUrl: true,
      flowType: 'pkce',
    },
  })
  return client.auth as unknown as AuthClient
}
