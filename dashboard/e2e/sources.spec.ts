/**
 * The Sources screen's per-source on/off switch against the real backend (SYNTHETIC seed):
 * every switch state drawn from the server fields, the owner's resume of the paused source (its
 * `source_paused` jobs are requeued by the server) and the pause back, keyboard operation, the
 * read-only switch of a reviewer, and phone width. The resume never enables a source and never
 * clears a technical block, so the disabled and blocked sources stay off.
 */
import { expect, test, type Locator, type Page } from '@playwright/test'
import { expectNoHorizontalScroll, guard, manifest, signIn } from './helpers.ts'

const SEEDED_PAUSE_REASON = 'SYNTHETIC owner pause for maintenance'

function card(page: Page, name: string): Locator {
  return page.locator('section', { has: page.getByRole('heading', { name, level: 2 }) })
}

function sourceSwitch(page: Page, name: string): Locator {
  return page.getByRole('switch', { name: `Collect from ${name}` })
}

test.describe('sources on/off switch', () => {
  test('every switch state comes from the server: running, paused, off in the configuration, blocked', async ({ page }) => {
    const { problems } = guard(page)
    await signIn(page, 'owner')
    await page.goto('/sources')
    const running = sourceSwitch(page, 'SYNTHETIC running source')
    await expect(running).toHaveAttribute('aria-checked', 'true')
    await expect(running).toBeEnabled()
    await expect(card(page, 'SYNTHETIC running source').getByText('Running', { exact: true })).toBeVisible()

    const paused = sourceSwitch(page, 'SYNTHETIC paused source')
    await expect(paused).toHaveAttribute('aria-checked', 'false')
    await expect(paused).toBeEnabled() // the owner may resume it
    await expect(card(page, 'SYNTHETIC paused source')).toContainText(SEEDED_PAUSE_REASON)

    const mk = sourceSwitch(page, 'SYNTHETIC mk source')
    await expect(mk).toHaveAttribute('aria-checked', 'false')
    await expect(mk).toBeDisabled() // aria-disabled: still focusable, announced as unavailable
    await expect(card(page, 'SYNTHETIC mk source')).toContainText('switched on only through its YAML file (activation step 7)')

    const blocked = sourceSwitch(page, 'SYNTHETIC blocked source')
    await expect(blocked).toHaveAttribute('aria-checked', 'false')
    await expect(blocked).toBeDisabled()
    await expect(card(page, 'SYNTHETIC blocked source')).toContainText('cleared only by the owner')

    // A disabled switch is reachable by keyboard but does nothing.
    await blocked.focus()
    await expect(blocked).toBeFocused()
    await page.keyboard.press('Space')
    await page.keyboard.press('Enter')
    await expect(page.getByRole('form')).toHaveCount(0)
    expect(problems).toEqual([])
  })

  test('owner resumes the paused source by keyboard: its waiting jobs are requeued; then pauses it again', async ({ page }) => {
    const data = manifest()
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'owner')
    await page.goto('/sources')
    const section = card(page, 'SYNTHETIC paused source')
    const toggle = sourceSwitch(page, 'SYNTHETIC paused source')
    await toggle.focus()
    await page.keyboard.press('Space')
    const reason = section.getByLabel(/Resume reason/)
    await expect(reason).toBeFocused()
    await expect(section.getByRole('button', { name: 'Resume this source' })).toBeDisabled()
    await reason.fill('SYNTHETIC E2E resume: maintenance finished')
    const answer = page.waitForResponse((r) => r.url().endsWith(`/api/sources/${data.sources.paused}/resume`) && r.request().method() === 'POST')
    await section.getByRole('button', { name: 'Resume this source' }).click()
    const resumed = await answer
    expect(resumed.status()).toBe(200)
    const sent = JSON.parse(resumed.request().postData() ?? '{}') as { idempotency_key: string; expected_version: number }
    expect(resumed.request().headers()['idempotency-key']).toBe(sent.idempotency_key)
    const body = (await resumed.json()) as { data: Record<string, unknown>; warnings: unknown[] }
    expect(body.data).toMatchObject({
      was_paused: true,
      paused: false,
      enabled: true,
      network_allowed: true,
      requeued_jobs: data.paused_source_waiting_jobs,
    })
    expect(body.warnings).toEqual([])
    await expect(section.getByTestId('source-resumed')).toHaveText(
      `Source resumed. ${data.paused_source_waiting_jobs} waiting jobs were requeued.`,
    )
    await expect(toggle).toHaveAttribute('aria-checked', 'true')
    await expect(section.getByText('Running', { exact: true })).toBeVisible()
    // The disabled and blocked sources were not touched by the switch.
    await expect(sourceSwitch(page, 'SYNTHETIC mk source')).toHaveAttribute('aria-checked', 'false')
    await expect(sourceSwitch(page, 'SYNTHETIC blocked source')).toHaveAttribute('aria-checked', 'false')

    // Switch it off again (the seeded state: paused, with the seeded reason).
    await toggle.click()
    await section.getByLabel(/Pause reason/).fill(SEEDED_PAUSE_REASON)
    await section.getByRole('button', { name: 'Pause this source' }).click()
    await expect(section.getByTestId('source-paused')).toHaveText('Source paused.')
    await expect(toggle).toHaveAttribute('aria-checked', 'false')
    const posts = apiRequests.filter((request) => request.method() === 'POST').map((request) => new URL(request.url()).pathname)
    expect(posts).toEqual([`/api/sources/${data.sources.paused}/resume`, `/api/sources/${data.sources.paused}/pause`])
    expect(problems).toEqual([])
  })

  test('a reviewer sees every switch read-only, with the reason', async ({ page }) => {
    await signIn(page, 'reviewer')
    await page.goto('/sources')
    await expect(sourceSwitch(page, 'SYNTHETIC running source')).toBeVisible()
    const switches = await page.getByRole('switch').all()
    expect(switches.length).toBeGreaterThanOrEqual(5) // one per registered source
    for (const toggle of switches) await expect(toggle).toBeDisabled()
    await expect(card(page, 'SYNTHETIC paused source')).toContainText('Resuming is owner-only')
    await expect(card(page, 'SYNTHETIC running source')).toContainText('Your role cannot pause sources')
  })
})

test.describe('sources switch at phone width', () => {
  test.use({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true })

  test('the switch and its open form fit without horizontal scroll', async ({ page }) => {
    const { problems } = guard(page)
    await signIn(page, 'owner')
    await page.goto('/sources')
    const toggle = sourceSwitch(page, 'SYNTHETIC running source')
    await expect(toggle).toBeVisible()
    await expectNoHorizontalScroll(page)
    await toggle.click()
    await expect(card(page, 'SYNTHETIC running source').getByLabel(/Pause reason/)).toBeVisible()
    await expectNoHorizontalScroll(page)
    await card(page, 'SYNTHETIC running source').getByRole('button', { name: 'Cancel' }).click()
    await expect(toggle).toBeFocused()
    expect(problems).toEqual([])
  })
})
