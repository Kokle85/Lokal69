// @vitest-environment node
/**
 * The dev-server/build guard of `vite.config.ts` (security review): a Supabase SECRET key must never
 * reach a browser bundle, whatever the mode and however it was pasted. Vite inlines every
 * `VITE_` value into the JavaScript it serves or builds, so the refusal has to happen before that.
 */
import type { ConfigEnv, UserConfig } from 'vite'
import { afterEach, describe, expect, it, vi } from 'vitest'
import viteConfig from '../../vite.config.ts'
import { guardBrowserEnvironment, isSecretKey, validateConfig } from '../configRules'

const config = viteConfig as (env: ConfigEnv) => UserConfig
const development: ConfigEnv = { command: 'serve', mode: 'development', isSsrBuild: false, isPreview: false }
const developmentBuild: ConfigEnv = { command: 'build', mode: 'development', isSsrBuild: false, isPreview: false }

function part(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString('base64url')
}

/** A SYNTHETIC unsigned JWT-shaped string (only its payload's `role` matters here). */
function jwt(payload: Record<string, unknown>): string {
  return `${part({ alg: 'HS256', typ: 'JWT' })}.${part(payload)}.c2lnbmF0dXJl`
}

const SECRET = 'sb_secret_SYNTHETIC_probe_0123456789'

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('vite.config.ts refuses a secret key in every mode', () => {
  it('a publishable key starts the dev server', () => {
    vi.stubEnv('VITE_SUPABASE_URL', 'http://127.0.0.1:54399')
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_SYNTHETIC_unit_test_key')
    expect(() => config(development)).not.toThrow()
  })

  it.each([
    ['leading space', ` ${SECRET}`],
    ['leading tab', `\t${SECRET}`],
    ['leading newline', `\n${SECRET}`],
    ['quoted', `"${SECRET}"`],
    ['upper case', SECRET.toUpperCase()],
    ['service_role JWT with whitespace', ` ${jwt({ role: 'service_role', iss: 'supabase' })} `],
  ])('a secret key with %s is refused for the dev server and a development build', (_name, value) => {
    vi.stubEnv('VITE_SUPABASE_URL', 'http://127.0.0.1:54399')
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', value)
    expect(() => config(development)).toThrow(/SECRET key/)
    expect(() => config(developmentBuild)).toThrow(/SECRET key/)
  })

  it('a secret key in the URL slot is refused too', () => {
    vi.stubEnv('VITE_SUPABASE_URL', SECRET)
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_SYNTHETIC_unit_test_key')
    expect(() => config(development)).toThrow(/SECRET key/)
  })

  it('an unexpected VITE_ variable is refused', () => {
    vi.stubEnv('VITE_SUPABASE_URL', 'http://127.0.0.1:54399')
    vi.stubEnv('VITE_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_SYNTHETIC_unit_test_key')
    vi.stubEnv('VITE_DATABASE_URL', 'postgresql://synthetic@127.0.0.1/none')
    expect(() => config(development)).toThrow(/unexpected VITE_ variables \(VITE_DATABASE_URL\)/)
  })
})

describe('shared secret-key rules (configRules)', () => {
  it('detects secret keys the same way at build time and at run time', () => {
    for (const value of [` ${SECRET}`, SECRET.toLowerCase(), jwt({ role: 'service_role' })]) {
      expect(isSecretKey(value)).toBe(true)
      expect(validateConfig({ VITE_SUPABASE_URL: 'https://p.supabase.co', VITE_SUPABASE_PUBLISHABLE_KEY: value }).ok).toBe(false)
      expect(() => guardBrowserEnvironment({ VITE_SUPABASE_PUBLISHABLE_KEY: value })).toThrow(/SECRET key/)
    }
    for (const value of ['sb_publishable_SYNTHETIC', jwt({ role: 'anon' }), '', undefined]) {
      expect(isSecretKey(value)).toBe(false)
    }
  })
})
