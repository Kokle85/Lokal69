/** Shared helpers for the E2E specs (SYNTHETIC users and data from tests/e2e/seed.py). */
import { execFileSync } from 'node:child_process'
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
  /** The SYNTHETIC spec v1.1 world (tests/e2e/seed_v11.py). */
  v11: {
    inquiries: Record<V11InquiryKey, string>
    replies: Record<'seller' | 'quarantined', string>
    references: Record<V11InquiryKey, string>
    listings: Record<V11InquiryKey, string>
    /** The REAL plan jobs' wait codes (`workers.inquiry_handlers._hold_code`). */
    wait_codes: Record<'waiting' | 'cooldown', string>
    worker_label: string
    retired_worker_label: string
    mailbox_id: string
    retired_mailbox_id: string
  }
}

export type V11InquiryKey = 'suppressed' | 'offline' | 'replied' | 'cooldown' | 'uncertain' | 'held' | 'waiting' | 'killswitch'

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

/**
 * No horizontal page scroll at the current viewport, and no content that only "fits" because it is
 * clipped: the document, the body and the main content area must all be as wide as the viewport.
 */
export async function expectNoHorizontalScroll(page: Page): Promise<void> {
  const widths = await page.evaluate(() => {
    const viewport = document.documentElement.clientWidth
    const main = document.querySelector('main')
    return {
      document: document.documentElement.scrollWidth - viewport,
      body: document.body.scrollWidth - viewport,
      main: main ? main.scrollWidth - main.clientWidth : 0,
      clipped: getComputedStyle(document.body).overflowX !== 'visible' || getComputedStyle(document.documentElement).overflowX !== 'visible',
    }
  })
  expect(widths).toEqual({ document: 0, body: 0, main: 0, clipped: false })
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

const REPO_ROOT = fileURLToPath(new URL('../../', import.meta.url))

/**
 * The concurrent change the owner did not see (TEST ONLY): one more `kill_switch` suppression is
 * recorded in the running backend's SYNTHETIC database through the REAL repository
 * (`tests/e2e/v11_actions.py`, loopback E2E databases only). Returns the new removable count.
 */
export function addKillSwitchSuppression(): number {
  const output = execFileSync('uv', ['run', '--quiet', 'python', 'tests/e2e/v11_actions.py', 'add-kill-switch-suppression'], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const last = output.trim().split('\n').at(-1) ?? '{}'
  const result = JSON.parse(last) as { created?: boolean; removable_suppressions?: number }
  if (result.created !== true || typeof result.removable_suppressions !== 'number') {
    throw new Error(`the SYNTHETIC suppression was not added: ${last}`)
  }
  return result.removable_suppressions
}

/**
 * A new revision of a SYNTHETIC listing arrives while a reviewer holds its case (TEST ONLY; spec 23
 * "new listing revision arriving before submit"): `tests/e2e/v11_actions.py add-listing-revision`
 * writes a promoted detail observation and revision (a changed price) into the running backend's
 * SYNTHETIC database (loopback E2E databases only).
 */
export function addListingRevision(key: string): { revision_id: string; revision_number: number } {
  const output = execFileSync('uv', ['run', '--quiet', 'python', 'tests/e2e/v11_actions.py', 'add-listing-revision', '--listing', key], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    timeout: 120_000,
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const last = output.trim().split('\n').at(-1) ?? '{}'
  const result = JSON.parse(last) as { revision_id?: string; revision_number?: number }
  if (typeof result.revision_id !== 'string' || typeof result.revision_number !== 'number') {
    throw new Error(`the SYNTHETIC listing revision was not added: ${last}`)
  }
  return { revision_id: result.revision_id, revision_number: result.revision_number }
}

/**
 * Spec 37.1 (standing authorization): no button or link anywhere offers to approve, send, resend or
 * answer a seller message.
 */
export async function expectNoApproveOrSendControl(page: Page): Promise<void> {
  const names = [
    ...(await page.getByRole('button').allTextContents()),
    ...(await page.getByRole('link').allTextContents()),
  ]
  expect(names.length).toBeGreaterThan(0)
  for (const name of names) expect(name).not.toMatch(/\b(approve|approval|send|resend|reply to|answer)\b/i)
}
