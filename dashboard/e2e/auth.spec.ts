import { expect, test } from '@playwright/test'
import { guard, manifest, MOCK_AUTH, signIn } from './helpers.ts'

test.describe('sign-in and session', () => {
  test('password sign-in lands on the overview and every API call is bearer-only', async ({ page }) => {
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'reviewer')
    await expect(page.getByTestId('workspace-name')).toContainText('SYNTHETIC E2E workspace')
    await expect(page.getByTestId('workspace-name')).toContainText('reviewer')
    expect(apiRequests.length).toBeGreaterThan(0)
    for (const request of apiRequests) {
      const headers = await request.allHeaders()
      expect(headers.authorization).toMatch(/^Bearer [A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/)
      expect(headers.apikey).toBeUndefined()
      expect(Object.values(headers).join(' ')).not.toContain('sb_publishable_')
      expect(headers.cookie).toBeUndefined()
    }
    expect(problems).toEqual([])
  })

  test('a wrong password is refused without detail', async ({ page }) => {
    await page.goto('/login')
    await page.getByLabel('Email').fill(manifest().users.reviewer.email)
    await page.getByLabel('Password').fill('not-the-password')
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByText('Sign-in failed. Check the email address and password.')).toBeVisible()
    await expect(page).toHaveURL(/\/login/)
  })

  test('deep links require sign-in and return to the requested page', async ({ page }) => {
    await page.goto('/candidates?status=pending')
    await expect(page).toHaveURL(/\/login\?next=/)
    await page.getByLabel('Email').fill(manifest().users.reviewer.email)
    await page.getByLabel('Password').fill(manifest().password)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Candidate queue' })).toBeVisible()
    await expect(page).toHaveURL(/\/candidates\?status=pending$/)
  })

  test('sign-out drops the session (supabase-js storage only) and protects the app again', async ({ page }) => {
    await signIn(page, 'reviewer')
    const keysBefore = await page.evaluate(() => Object.keys(window.localStorage))
    // The session lives only in supabase-js' own storage key (sb-<ref>-auth-token).
    expect(keysBefore.filter((key) => /token/i.test(key)).every((key) => key.startsWith('sb-'))).toBe(true)
    const sessionStorageKeys = await page.evaluate(() => Object.keys(window.sessionStorage))
    expect(sessionStorageKeys.filter((key) => /token/i.test(key))).toEqual([])
    await page.getByRole('button', { name: 'Sign out' }).click()
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
    const keysAfter = await page.evaluate(() => Object.keys(window.localStorage).filter((key) => key.startsWith('sb-') && key.endsWith('auth-token')))
    expect(keysAfter).toEqual([])
    await page.goto('/reviews')
    await expect(page).toHaveURL(/\/login/)
  })

  test('magic link (PKCE) signs in through the emulated email link and cleans the URL', async ({ page, request }) => {
    const { problems } = guard(page)
    await page.goto('/login')
    await page.getByLabel('Email').fill(manifest().users.owner.email)
    await page.getByRole('button', { name: 'Email me a sign-in link' }).click()
    await expect(page.getByText(/a sign-in link is on its way/)).toBeVisible()
    const link = await request.get(`${MOCK_AUTH}/__e2e/magic-link?email=${encodeURIComponent(manifest().users.owner.email)}`)
    expect(link.ok()).toBe(true)
    const { url } = (await link.json()) as { url: string }
    await page.goto(url)
    await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()
    expect(page.url()).not.toContain('code=')
    await expect(page.getByTestId('workspace-name')).toContainText('owner')
    expect(problems).toEqual([])
  })

  test('a cancelled or expired magic link shows an inert error and does not sign in', async ({ page }) => {
    const { problems } = guard(page)
    await page.goto(
      '/auth/callback?error=access_denied&error_code=otp_expired&error_description=%3Cimg%20src%3Dx%20onerror%3Dalert(1)%3E%20Call%20%2B1%20555%200100',
    )
    await expect(page.getByRole('heading', { name: 'Sign-in not completed' })).toBeVisible()
    await expect(page.getByRole('alert')).toContainText('Error code otp_expired.')
    // The attacker-controllable description is never shown (no HTML, no spoofed text).
    await expect(page.locator('img')).toHaveCount(0)
    await expect(page.locator('body')).not.toContainText('onerror')
    await expect(page.locator('body')).not.toContainText('555 0100')
    expect(page.url()).not.toContain('error_description')
    await page.getByRole('link', { name: 'Back to sign in' }).click()
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible()
    expect(problems).toEqual([])
  })

  test('a user with two memberships chooses the workspace; X-Workspace-Id follows the choice', async ({ page }) => {
    const data = manifest()
    await signIn(page, 'multi', { expectOverview: false })
    await expect(page.getByRole('heading', { name: 'Choose a workspace' })).toBeVisible()
    const overviewRequest = page.waitForRequest((request) => request.url().endsWith('/api/overview'))
    await page.getByRole('button', { name: /SYNTHETIC E2E second workspace/ }).click()
    await expect(page.getByRole('heading', { name: 'Overview', level: 1 })).toBeVisible()
    await expect(page.getByTestId('workspace-name')).toContainText('second workspace')
    await expect(page.getByTestId('workspace-name')).toContainText('viewer')
    expect((await overviewRequest).headers()['x-workspace-id']).toBe(data.second_workspace_id)
  })

  test('an authenticated stranger without membership is refused with a correlation id', async ({ page }) => {
    await signIn(page, 'stranger', { expectOverview: false })
    await expect(page.getByRole('heading', { name: 'Workspace unavailable' })).toBeVisible()
    await expect(page.getByTestId('correlation-id')).not.toBeEmpty()
  })
})
