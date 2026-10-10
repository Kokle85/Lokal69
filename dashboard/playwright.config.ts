/**
 * Browser E2E against REAL local services (all data SYNTHETIC, nothing leaves 127.0.0.1):
 *
 *   1. mock Supabase Auth  http://127.0.0.1:54399  tests/e2e/mock_supabase_auth.py (ES256 JWKS)
 *   2. backend API         http://127.0.0.1:8765   tests/e2e/run_backend.py (create_app on a fresh,
 *                                                   migrated, seeded PostgreSQL database; review
 *                                                   claim lease 60 s for the real expiry test)
 *   3. dashboard           http://127.0.0.1:4173   `vite build` (with the mock as VITE_SUPABASE_URL and
 *                                                   the strict CSP) served by `vite preview`, whose
 *                                                   /api proxy forwards to the backend, so the browser
 *                                                   calls the API same-origin exactly as in production.
 *
 * Playwright 1.56.1 uses the preinstalled chromium-1194 (PLAYWRIGHT_BROWSERS_PATH); never run
 * `playwright install`. Tests run serially: they share one seeded database.
 */
import { defineConfig, devices } from '@playwright/test'

const MOCK_AUTH = 'http://127.0.0.1:54399'
const API = 'http://127.0.0.1:8765'
const APP = 'http://127.0.0.1:4173'
const graceful = { signal: 'SIGTERM' as const, timeout: 10_000 }

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['html', { open: 'never', outputFolder: 'playwright-report' }]],
  outputDir: 'test-results',
  use: {
    baseURL: APP,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `uv run python tests/e2e/mock_supabase_auth.py --port 54399 --allow-origin ${APP}`,
      cwd: '..',
      url: `${MOCK_AUTH}/auth/v1/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      gracefulShutdown: graceful,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command: `uv run python tests/e2e/run_backend.py --port 8765 --supabase-url ${MOCK_AUTH} --app-origin ${APP} --claim-duration-seconds 60`,
      cwd: '..',
      url: `${API}/readyz`,
      reuseExistingServer: false,
      timeout: 240_000,
      gracefulShutdown: graceful,
      stdout: 'pipe',
      stderr: 'pipe',
    },
    {
      command:
        'npx vite build --outDir dist-e2e --emptyOutDir && node scripts/check-security.mjs --dist && npx vite preview --outDir dist-e2e',
      url: APP,
      reuseExistingServer: false,
      timeout: 180_000,
      gracefulShutdown: graceful,
      env: {
        VITE_SUPABASE_URL: MOCK_AUTH,
        VITE_SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_SYNTHETIC_e2e_key',
        DASHBOARD_API_TARGET: API,
        DASHBOARD_OUT_DIR: 'dist-e2e',
      },
    },
  ],
})
