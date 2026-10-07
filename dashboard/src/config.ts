/**
 * Browser configuration. Only TWO variables exist, both public by design:
 *
 * - `VITE_SUPABASE_URL`: the Supabase project URL (Auth only; the browser never queries tables);
 * - `VITE_SUPABASE_PUBLISHABLE_KEY`: the publishable key, sent to Supabase Auth only, never to the
 *   dashboard backend.
 *
 * A secret key (`sb_secret_...` or a legacy `service_role` JWT) is refused outright: it would bypass
 * row-level security if it ever reached a browser. `vite.config.ts` applies the same checks at build.
 */
export interface DashboardConfig {
  supabaseUrl: string
  publishableKey: string
}

export type ConfigResult = { ok: true; config: DashboardConfig } | { ok: false; problems: string[] }

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
  } else if (/^sb_secret_/i.test(key) || decodeJwtRole(key) === 'service_role') {
    problems.push('VITE_SUPABASE_PUBLISHABLE_KEY holds a SECRET key. Use the publishable key; never ship a secret key.')
  }
  if (problems.length) return { ok: false, problems }
  return { ok: true, config: { supabaseUrl: url.replace(/\/+$/, ''), publishableKey: key } }
}

export function readConfig(): ConfigResult {
  return validateConfig({
    VITE_SUPABASE_URL: import.meta.env.VITE_SUPABASE_URL as string | undefined,
    VITE_SUPABASE_PUBLISHABLE_KEY: import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as string | undefined,
  })
}
