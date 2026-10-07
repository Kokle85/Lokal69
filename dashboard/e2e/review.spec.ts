import { expect, test, type Request } from '@playwright/test'
import { claimCase, fillWatchDecision, guard, hold, manifest, signIn } from './helpers.ts'

const SUBMIT = '**/api/reviews/*/submit'

test.describe.configure({ mode: 'serial' })

test.describe('review workflow', () => {
  test('a reviewer claims and submits "watch"; a second reviewer sees ALREADY_CLAIMED, then VERSION_CONFLICT', async ({ browser }) => {
    const data = manifest()
    const first = await browser.newContext()
    const second = await browser.newContext()
    const pageA = await first.newPage()
    const pageB = await second.newPage()
    const guardA = guard(pageA)
    const guardB = guard(pageB)
    try {
      await signIn(pageA, 'reviewer')
      await pageA.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Reviews' }).click()
      await pageA.getByRole('link', { name: 'Example Trail' }).first().waitFor()
      await pageA.goto(`/reviews/${data.cases.alpha}`)
      await claimCase(pageA)

      await signIn(pageB, 'reviewer2')
      await pageB.goto(`/reviews/${data.cases.alpha}`)
      await expect(pageB.getByTestId('claim-state')).toContainText('Claimed by another reviewer')
      await pageB.getByRole('button', { name: 'Claim' }).click()
      await expect(pageB.getByText('Already claimed by another reviewer')).toBeVisible()
      await expect(pageB.getByTestId('correlation-id').first()).not.toBeEmpty()
      await expect(pageB.getByRole('button', { name: 'Reload the case' })).toBeVisible()

      await fillWatchDecision(pageA, 'SYNTHETIC E2E: price in band; watch for a further drop.')
      await pageA.getByRole('button', { name: 'Submit decision' }).click()
      await expect(pageA.getByTestId('decision-saved')).toContainText('Decision saved: watch')
      await expect(pageA.getByTestId('decision-entry')).toHaveCount(1)

      // B still shows the old case version: its next claim is refused as a version conflict.
      await pageB.getByRole('button', { name: 'Claim' }).click()
      await expect(pageB.getByText('The case or listing changed')).toBeVisible()
      await pageB.getByRole('button', { name: 'Reload the case' }).click()
      await expect(pageB.getByTestId('decision-entry')).toHaveCount(1)
      await expect(pageB.locator('#case')).toContainText('watch')
      expect(guardA.problems).toEqual([])
      expect(guardB.problems).toEqual([])
    } finally {
      await first.close()
      await second.close()
    }
  })

  test('token expiry mid-review: the backend refuses the stale token, the client refreshes and retries the same request', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    const submits: Array<{ request: Request; status: number }> = []
    page.on('response', (response) => {
      if (response.url().endsWith('/submit') && response.request().method() === 'POST') {
        submits.push({ request: response.request(), status: response.status() })
      }
    })
    await signIn(page, 'expiring')
    await page.goto(`/reviews/${data.cases.bravo}`)
    await claimCase(page)
    await fillWatchDecision(page, 'SYNTHETIC E2E: token expired while this review was being written.')
    // The access token of this SYNTHETIC user really lives 6 s (its response advertised an hour).
    await page.waitForTimeout(7_500)
    await page.getByRole('button', { name: 'Submit decision' }).click()
    await expect(page.getByTestId('decision-saved')).toContainText('Decision saved: watch')
    expect(submits.map((item) => item.status)).toEqual([401, 201])
    const [stale, retried] = submits
    expect(stale!.request.headers()['idempotency-key']).toBe(retried!.request.headers()['idempotency-key'])
    expect(stale!.request.postData()).toBe(retried!.request.postData())
    expect(stale!.request.headers().authorization).not.toBe(retried!.request.headers().authorization)
    await expect(page.getByTestId('decision-entry')).toHaveCount(1)
    expect(problems).toEqual([])
  })

  test('network loss after the write: "not confirmed", then an idempotent retry resolves to exactly one decision', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    const sent: Request[] = []
    page.on('request', (request) => {
      if (request.url().endsWith('/submit') && request.method() === 'POST') sent.push(request)
    })
    await signIn(page, 'reviewer')
    await page.goto(`/reviews/${data.cases.charlie}`)
    await claimCase(page)
    await fillWatchDecision(page, 'SYNTHETIC E2E: the connection dropped right after this was sent.')
    let committed = 0
    await page.route(
      SUBMIT,
      async (route) => {
        const response = await route.fetch() // the server receives and commits the decision...
        committed = response.status()
        await route.abort('internetdisconnected') // ...but the browser never sees the answer
      },
      { times: 1 },
    )
    await page.getByRole('button', { name: 'Submit decision' }).click()
    await expect(page.getByTestId('mutation-unconfirmed')).toContainText('Not confirmed: it may or may not have been saved')
    expect(committed).toBe(201)
    await expect(page.getByTestId('decision-saved')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Submit decision' })).toBeDisabled()
    await page.getByRole('button', { name: 'Retry the same request' }).click()
    await expect(page.getByTestId('decision-saved')).toContainText('Decision saved: watch')
    await expect(page.locator('[data-testid="mutation-confirmed"]', { has: page.getByTestId('decision-saved') })).toContainText(
      'confirmed by the server on an idempotent retry',
    )
    expect(sent).toHaveLength(2)
    expect(sent[0]!.headers()['idempotency-key']).toBe(sent[1]!.headers()['idempotency-key'])
    expect(sent[0]!.postData()).toBe(sent[1]!.postData())
    await expect(page.getByTestId('decision-entry')).toHaveCount(1)
    expect(problems).toEqual([])
  })

  test('a page reloaded while a decision is in flight reports the server state and never resubmits', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    const posts: string[] = []
    page.on('request', (request) => {
      if (request.url().endsWith('/submit') && request.method() === 'POST') posts.push(request.url())
    })
    await signIn(page, 'reviewer')
    await page.goto(`/reviews/${data.cases.delta}`)
    await claimCase(page)
    await fillWatchDecision(page, 'SYNTHETIC E2E: the page was reloaded while this was in flight.')
    let reachedServer = 0
    const held = hold()
    await page.route(SUBMIT, async (route) => {
      reachedServer += 1
      await route.fetch() // committed on the server; the browser is kept waiting
      await held.promise
      await route.abort().catch(() => undefined)
    })
    await page.getByRole('button', { name: 'Submit decision' }).click()
    await expect(page.getByTestId('mutation-pending')).toContainText('Not saved yet')
    await expect.poll(() => reachedServer).toBe(1)
    await page.reload()
    held.release()
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await expect(page.getByTestId('earlier-submission')).toContainText('Nothing was resent automatically.')
    await expect(page.getByTestId('earlier-submission-recorded')).toContainText('The server recorded it: watch')
    await page.waitForTimeout(1_500)
    expect(posts).toHaveLength(1)
    await expect(page.getByTestId('decision-entry')).toHaveCount(1)
    await expect(page.getByTestId('claim-state')).toContainText('Not claimed')
    expect(problems).toEqual([])
  })
})
