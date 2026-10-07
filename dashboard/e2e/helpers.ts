/** Shared helpers for the E2E specs (SYNTHETIC users and data from tests/e2e/seed.py). */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { expect, type Page, type Request } from '@playwright/test'

export interface Manifest {
  workspace_id: string
  second_workspace_id: string
  password: string
  users: Record<UserKey, { email: string; user_id: string }>
  listings: Record<string, string>
  cases: Record<string, string>
  valuations: Record<string, string>
  comparable_set_id: string
  titles: Record<string, string>
  sources: Record<string, string>
}

export type UserKey = 'owner' | 'reviewer' | 'reviewer2' | 'viewer' | 'expiring' | 'multi' | 'stranger'

export const MOCK_AUTH = 'http://127.0.0.1:54399'
export const PUBLISHABLE_KEY = 'sb_publishable_SYNTHETIC_e2e_key'

let cached: Manifest | null = null

/** Written by tests/e2e/run_backend.py once the database is seeded (before the backend is ready). */
export function manifest(): Manifest {
  cached ??= JSON.parse(
    readFileSync(fileURLToPath(new URL('./.generated/seed-manifest.json', import.meta.url)), 'utf8'),
  ) as Manifest
  return cached
}

/** Fails the test on page errors, CSP violations and any JavaScript dialog (e.g. an XSS alert). */
export function guard(page: Page): { problems: string[]; apiRequests: Request[] } {
  const problems: string[] = []
  const apiRequests: Request[] = []
  page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`))
  page.on('console', (message) => {
    const text = message.text()
    if (/Content Security Policy|Refused to (execute|load|connect|apply)/i.test(text)) problems.push(`csp: ${text}`)
  })
  page.on('dialog', (dialog) => {
    problems.push(`dialog: ${dialog.type()} ${dialog.message()}`)
    void dialog.dismiss()
  })
  page.on('request', (request) => {
    if (new URL(request.url()).pathname.startsWith('/api/')) apiRequests.push(request)
  })
  return { problems, apiRequests }
}

export async function signIn(page: Page, user: UserKey, options: { expectOverview?: boolean } = {}): Promise<void> {
  const data = manifest()
  await page.goto('/login')
  await page.getByLabel('Email').fill(data.users[user].email)
  await page.getByLabel('Password').fill(data.password)
  await page.getByRole('button', { name: 'Sign in' }).click()
  if (options.expectOverview ?? true) {
    await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()
  }
}

export async function expectNoXss(page: Page): Promise<void> {
  const probe = await page.evaluate(() => (window as unknown as { xssProbe?: unknown }).xssProbe)
  expect(probe).toBeUndefined()
  await expect(page.locator('img[src="x"], iframe, svg[onload], [onerror]')).toHaveCount(0)
  const scriptHrefs = await page.locator('a[href^="javascript:" i]').count()
  expect(scriptHrefs).toBe(0)
}

/** No horizontal page scroll at the current viewport. */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(0)
}

export async function claimCase(page: Page): Promise<void> {
  await page.getByRole('button', { name: /^(Claim|Claim again)$/ }).click()
  await expect(page.getByTestId('claim-state')).toContainText('You hold the claim until')
}

export async function fillWatchDecision(page: Page, summary: string): Promise<void> {
  await page.getByRole('radio', { name: /Watch/ }).check()
  await page.getByRole('checkbox', { name: 'price in band' }).check()
  await page.getByLabel(/^Summary/).fill(summary)
}

/** A promise the test resolves later (to keep a routed request waiting). */
export function hold(): { promise: Promise<void>; release: () => void } {
  const holder: { resolve?: () => void } = {}
  const promise = new Promise<void>((resolve) => {
    holder.resolve = resolve
  })
  return { promise, release: () => holder.resolve?.() }
}
