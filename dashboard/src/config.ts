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
import { validateConfig, type ConfigResult } from './configRules'

export { validateConfig, type ConfigResult, type DashboardConfig } from './configRules'

export function readConfig(): ConfigResult {
  return validateConfig({
    VITE_SUPABASE_URL: import.meta.env.VITE_SUPABASE_URL as string | undefined,
    VITE_SUPABASE_PUBLISHABLE_KEY: import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY as string | undefined,
  })
}
