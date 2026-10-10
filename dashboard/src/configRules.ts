/**
 * Validation rules of the two public browser variables, shared by the app (`config.ts`, which shows
 * a "not configured" page) and the production build (`vite.config.ts`, which refuses to build).
 * Pure code: no `import.meta`, no DOM access.
 *
 * A secret key (`sb_secret_...` or a legacy `service_role` JWT) is refused outright: it would bypass
 * row-level security if it ever reached a browser.
 */
export interface DashboardConfig {
  supabaseUrl: string
  publishableKey: string
}

export type ConfigResult = { ok: true; config: DashboardConfig } | { ok: false; problems: string[] }

/** The ONLY variables that may reach the browser bundle (Vite inlines every `VITE_` value). */
export const BROWSER_VARIABLES = ['VITE_SUPABASE_URL', 'VITE_SUPABASE_PUBLISHABLE_KEY'] as const

const LOOPBACK = new Set(['127.0.0.1', 'localhost', '[::1]'])

function decodeJwtRole(key: string): string | null {
  const parts = key.split('.')
  if (parts.length !== 3 || !parts[1]) return null
  try {
    const json = atob(parts[1].replace(/-/g, '+').replace(/_/g, '/'))
    const payload = JSON.parse(json) as { role?: unknown }
    return typeof payload.role === 'string' ? payload.role : null
  } catch {
    return null
  }
}

/**
 * A Supabase SECRET key in any form a copy-paste can produce: an `sb_secret_` key anywhere in the
 * value (surrounding whitespace or quotes, any case) or a legacy `service_role` JWT. A publishable
 * key never contains `sb_secret_`, so the search is deliberately not anchored.
 */
export function isSecretKey(value: string | null | undefined): boolean {
  const text = (value ?? '').trim().replace(/^["']+|["']+$/g, '')
  return /sb_secret_/i.test(text) || decodeJwtRole(text) === 'service_role'
}

/**
 * The guard `vite.config.ts` applies before the dev server, `vite preview` or ANY build (every mode)
 * starts, because Vite inlines every `VITE_` value into the JavaScript it serves: only the two public
 * variables may exist, and neither may hold a secret key.
 */
export function guardBrowserEnvironment(env: Record<string, string | undefined>): void {
  const allowed = new Set<string>(BROWSER_VARIABLES)
  const unexpected = Object.keys(env).filter((name) => name.startsWith('VITE_') && !allowed.has(name))
  if (unexpected.length) {
    throw new Error(
      `Refusing to start: unexpected VITE_ variables (${unexpected.join(', ')}). Only ${BROWSER_VARIABLES.join(
        ' and ',
      )} may be exposed to the browser.`,
    )
  }
  for (const name of BROWSER_VARIABLES) {
    if (isSecretKey(env[name])) throw new Error(`Refusing to start: ${name} holds a Supabase SECRET key.`)
  }
}

export function validateConfig(env: Record<string, string | undefined>): ConfigResult {
  const problems: string[] = []
  const url = (env.VITE_SUPABASE_URL ?? '').trim()
  const key = (env.VITE_SUPABASE_PUBLISHABLE_KEY ?? '').trim()
  let parsed: URL | null = null
  if (!url) {
    problems.push('VITE_SUPABASE_URL is not set.')
  } else {
    try {
      parsed = new URL(url)
    } catch {
      problems.push('VITE_SUPABASE_URL is not a valid URL.')
    }
  }
  if (parsed) {
    if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') {
      problems.push('VITE_SUPABASE_URL must use https.')
    } else if (parsed.protocol === 'http:' && !LOOPBACK.has(parsed.hostname)) {
      problems.push('VITE_SUPABASE_URL must use https outside local development.')
    }
    if (parsed.username || parsed.password || parsed.search || parsed.hash) {
      problems.push('VITE_SUPABASE_URL must not contain credentials, a query or a fragment.')
    }
  }
  if (!key) {
    problems.push('VITE_SUPABASE_PUBLISHABLE_KEY is not set.')
  } else if (isSecretKey(key)) {
    problems.push('VITE_SUPABASE_PUBLISHABLE_KEY holds a SECRET key. Use the publishable key; never ship a secret key.')
  }
  if (problems.length) return { ok: false, problems }
  return { ok: true, config: { supabaseUrl: url.replace(/\/+$/, ''), publishableKey: key } }
}

/**
 * The production-build gate (`vite.config.ts`): a bundle without a valid public configuration would
 * only ever show "not configured", so the build refuses instead (development keeps that page).
 */
export function requireProductionConfig(env: Record<string, string | undefined>): DashboardConfig {
  const result = validateConfig(env)
  if (!result.ok) {
    throw new Error(
      `Refusing to build for production: ${result.problems.join(' ')} Set VITE_SUPABASE_URL and ` +
        'VITE_SUPABASE_PUBLISHABLE_KEY (dashboard/README.md); `vite build --mode development` keeps the in-app ' +
        '"not configured" page instead.',
    )
  }
  return result.config
}
