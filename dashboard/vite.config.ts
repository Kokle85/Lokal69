/// <reference types="vitest/config" />
/**
 * Vite configuration of the private review dashboard.
 *
 * - Only TWO VITE_ variables may exist (VITE_SUPABASE_URL, VITE_SUPABASE_PUBLISHABLE_KEY); any other
 *   VITE_ variable, or a Supabase SECRET key in either of them (also padded, quoted or in another
 *   case: `configRules.isSecretKey`), fails the dev server, preview and every build mode.
 * - A PRODUCTION build (`vite build`, mode `production`) also fails when either required variable is
 *   missing or invalid (`src/configRules.ts`), so a misconfigured bundle is never shipped. The dev
 *   server, tests and `vite build --mode development` keep the in-app "not configured" page.
 * - Screens are code-split (`src/App.tsx`); supabase-js stays in the entry chunk because the session
 *   restore needs it immediately.
 * - `/api` is proxied to DASHBOARD_API_TARGET (Node-side only, never exposed to the bundle) by both the
 *   dev server and `vite preview`, so the browser always calls the API same-origin.
 * - Production builds get a strict Content-Security-Policy <meta> (no inline scripts or styles;
 *   connections only to the same origin and the Supabase project). Development keeps Vite's inline
 *   React refresh preamble, so the policy is applied to builds only.
 */
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import { guardBrowserEnvironment, requireProductionConfig } from './src/configRules.ts'

function originOf(value: string | undefined): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.origin : null
  } catch {
    return null
  }
}

function contentSecurityPolicy(supabaseOrigin: string | null): Plugin {
  const connect = ["'self'", supabaseOrigin].filter(Boolean).join(' ')
  const policy = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self'",
    "font-src 'self'",
    `connect-src ${connect}`,
    "object-src 'none'",
    "base-uri 'none'",
    "form-action 'self'",
    "frame-src 'none'",
    "manifest-src 'self'",
    "worker-src 'none'",
  ].join('; ')
  return {
    name: 'suv-dashboard-csp',
    apply: 'build',
    transformIndexHtml() {
      return [{ tag: 'meta', attrs: { 'http-equiv': 'Content-Security-Policy', content: policy }, injectTo: 'head-prepend' }]
    },
  }
}

export default defineConfig(({ mode, command }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  // Every mode (dev server, preview, development or production build): Vite inlines VITE_ values.
  guardBrowserEnvironment(env)
  if (command === 'build' && mode === 'production') requireProductionConfig(env)
  // Node-side only (never in the bundle: Vite exposes VITE_ variables only). Read from the process
  // environment or from the .env files, as .env.example documents.
  const apiTarget = loadEnv(mode, process.cwd(), 'DASHBOARD_').DASHBOARD_API_TARGET || 'http://127.0.0.1:8000'
  const proxy = { '/api': { target: apiTarget, changeOrigin: false, secure: true } }
  const securityHeaders = {
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'X-Frame-Options': 'DENY',
  }
  return {
    plugins: [react(), contentSecurityPolicy(originOf(env.VITE_SUPABASE_URL))],
    server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy, headers: securityHeaders },
    preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy, headers: securityHeaders },
    // Screens are separate chunks; supabase-js dominates the entry chunk (~570 kB, ~170 kB gzip).
    build: { sourcemap: false, target: 'es2023', chunkSizeWarningLimit: 700 },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      include: ['src/**/*.test.{ts,tsx}'],
      restoreMocks: true,
      env: {
        VITE_SUPABASE_URL: 'http://127.0.0.1:54399',
        VITE_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_SYNTHETIC_unit_test_key',
      },
    },
  }
})
