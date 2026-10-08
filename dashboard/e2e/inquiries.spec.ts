/**
 * Spec v1.1 screens against the REAL backend and the SYNTHETIC v1.1 world (tests/e2e/seed_v11.py):
 * seller inquiries, replies, inquiry control, mail-worker health, lags and the 15-day evaluation.
 * Nothing is ever sent: the E2E backend runs no worker and every address is `example.invalid`.
 */
import { expect, test } from '@playwright/test'
import { expectNoApproveOrSendControl, expectNoHorizontalScroll, guard, manifest, signIn } from './helpers.ts'

test.describe('seller inquiries and replies', () => {
  test('reviewer: attention groups, the caps wait and the list, with no approve or send control', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await signIn(page, 'reviewer')
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Seller inquiries' }).click()
    await expect(page.getByRole('heading', { name: 'Seller inquiries', level: 1 })).toBeVisible()
    await expect(page.getByTestId('standing-authorization')).toContainText('no per-message approval')
    await expect(page.getByTestId('attention-uncertain')).toContainText(data.v11.references.uncertain)
    await expect(page.getByTestId('attention-uncertain')).toContainText('delivery uncertain')
    await expect(page.getByTestId('attention-held')).toContainText(data.v11.references.held)
    await expect(page.getByTestId('attention-suppressed')).toContainText(data.v11.references.suppressed)
    await expect(page.getByTestId('attention-suppressed')).toContainText('seller opted out')
    const waiting = page.getByTestId('attention-waiting')
    await expect(waiting).toContainText(data.v11.references.waiting)
    await expect(waiting).toContainText('The rolling caps are reached (2 of 2 in 24 h')
    const list = page.getByRole('table', { name: 'Seller inquiries' })
    await expect(list.getByRole('link', { name: new RegExp(data.v11.references.replied) })).toBeVisible()
    await expectNoApproveOrSendControl(page)

    const total = await list.getByTestId('inquiry-row').count()
    expect(total).toBeGreaterThan(1)
    await page.getByLabel('State').selectOption('replied')
    await page.getByRole('button', { name: 'Apply filters' }).click()
    await expect(page).toHaveURL(/\/inquiries\?state=replied$/)
    await expect(list.getByTestId('inquiry-row')).toHaveCount(1)
    // Back: the list AND the filter form show the unfiltered state again (never a stale filter).
    await page.goBack()
    await expect(page).toHaveURL(/\/inquiries$/)
    await expect(page.getByLabel('State')).toHaveValue('')
    await expect(list.getByTestId('inquiry-row')).toHaveCount(total)
    expect(problems).toEqual([])
  })

  test('reviewer: inquiry detail with the informational Macedonian preview, a withheld address and the timeline', async ({ page }) => {
    const data = manifest()
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'reviewer')
    await page.goto(`/inquiries/${data.v11.inquiries.replied}`)
    await expect(page.getByRole('heading', { name: new RegExp(`Inquiry: ${data.v11.references.replied}`) })).toBeVisible()
    await expect(page.getByTestId('original-message')).toContainText('Ist das Fahrzeug noch verfügbar?')
    await expect(page.getByTestId('mk-preview')).toContainText('informational only')
    await expect(page.getByTestId('mk-preview-note')).toContainText('not an approval draft')
    await expect(page.getByTestId('mk-preview')).toContainText('Дали возилото е сè уште достапно?')
    await expect(page.getByTestId('recipient-address-withheld')).toBeVisible()
    await expect(page.locator('main')).not.toContainText('@example.invalid')
    await expect(page.getByTestId('approval-required')).toContainText('no per-message approval exists')
    await expect(page.getByRole('table', { name: 'Send attempts' })).toContainText('accepted')
    await expect(page.getByTestId('inquiry-timeline')).toContainText('Accepted by the provider (not delivery)')
    await expect(page.getByTestId('inquiry-timeline')).toContainText('Seller replied')
    const source = page.getByRole('link', { name: /Open the source listing/ })
    await expect(source).toHaveAttribute('rel', 'noopener noreferrer')
    await expect(page.getByRole('table', { name: 'Replies' }).getByTestId('reply-row')).toHaveCount(2)
    await expectNoApproveOrSendControl(page)
    // Only GETs: an inquiry page never writes.
    expect(apiRequests.filter((request) => request.method() !== 'GET')).toEqual([])
    expect(problems).toEqual([])
  })

  test('reviewer: escalations needing an owner decision, an unaccepted quote, and a quarantined reply as metadata only', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await signIn(page, 'reviewer')
    await page.goto(`/replies/${data.v11.replies.seller}`)
    const escalations = page.getByTestId('escalations')
    await expect(escalations).toContainText('Needs your decision')
    await expect(escalations.getByTestId('escalation')).toHaveText([
      'payment: the seller asks for a payment or deposit.',
      'reservation: the seller proposes a reservation.',
    ])
    const quote = page.getByTestId('price-quote')
    await expect(quote).toContainText('EUR 26,500')
    await expect(quote).toContainText('final or lowest')
    await expect(quote).toContainText('unaccepted seller quote')
    await expect(page.getByTestId('reply-original')).toContainText('Unser letzter Preis ist 26.500 EUR')
    await expect(page.getByTestId('reply-mk-summary')).toContainText('непотврдена понуда')
    await expect(page.getByRole('table', { name: 'Document statements' })).toContainText('registration')
    await expect(page.getByTestId('sender-address-withheld')).toBeVisible()
    await expectNoApproveOrSendControl(page)

    await page.goto(`/replies/${data.v11.replies.quarantined}`)
    await expect(page.getByTestId('quarantine-notice')).toContainText('forwarded_message')
    await expect(page.getByTestId('content-withheld')).toContainText('withheld until the owner verifies it')
    await expect(page.getByTestId('reply-original')).toHaveCount(0)
    await expect(page.locator('main')).not.toContainText('Weitergeleitet')

    await page.goto('/replies?quarantined_only=true')
    const quarantinedRows = page.getByRole('table', { name: 'Seller replies' }).getByTestId('reply-row')
    await expect(quarantinedRows).toHaveCount(1)
    // A claim derived from the withheld text is withheld too.
    await expect(quarantinedRows.first().getByTestId('availability-withheld')).toHaveText('withheld (unverified match)')
    expect(problems).toEqual([])
  })

  test('owner: sees the recipient address and verifies the quarantined reply text', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await signIn(page, 'owner')
    await page.goto(`/inquiries/${data.v11.inquiries.replied}`)
    await expect(page.getByTestId('recipient-address')).toContainText(/@example\.invalid$/)
    await page.goto(`/replies/${data.v11.replies.quarantined}`)
    await expect(page.getByTestId('quarantine-notice')).toBeVisible()
    await expect(page.getByTestId('content-withheld')).toHaveCount(0)
    await expect(page.getByTestId('reply-original')).toContainText('Weitergeleitet')
    await expect(page.getByTestId('sender-address')).toContainText(/@example\.invalid$/)
    // Its deposit request is never presented as the seller's (payment-fraud safety).
    const escalations = page.getByTestId('escalations')
    await expect(escalations).toContainText('sender not verified as the seller')
    await expect(escalations).toContainText('Do not pay, reserve or send documents')
    await expect(escalations.getByTestId('escalation')).toHaveText(['payment: the sender asks for a payment or deposit.'])
    await expect(escalations).not.toContainText('the seller asks')
    await expectNoApproveOrSendControl(page)
    expect(problems).toEqual([])
  })

  test("owner: the Slack signal's dashboard link opens the reply after sign-in", async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    // The backend's link form (domain.replies.dashboard_reply_url) for the seller-reply signal and
    // the "decision needed" owner alert.
    await page.goto(`/inquiries/${data.v11.inquiries.replied}/replies/${data.v11.replies.seller}`)
    await expect(page).toHaveURL(/\/login\?next=/)
    await page.getByLabel('Email').fill(data.users.owner.email)
    await page.getByLabel('Password').fill(data.password)
    await page.getByRole('button', { name: 'Sign in' }).click()
    await expect(page.getByRole('heading', { name: new RegExp(`^Reply: ${data.v11.references.replied}`), level: 1 })).toBeVisible()
    await expect(page).toHaveURL(new RegExp(`/inquiries/${data.v11.inquiries.replied}/replies/${data.v11.replies.seller}$`))
    await expect(page.getByTestId('escalations')).toContainText('Needs your decision')
    await expect(page.getByTestId('escalations').getByTestId('escalation').first()).toContainText('the seller asks')
    await expect(page.getByRole('heading', { name: 'Page not found' })).toHaveCount(0)
    await expectNoApproveOrSendControl(page)
    expect(problems).toEqual([])
  })

  test('held and uncertain inquiries explain themselves without any approval wait', async ({ page }) => {
    const data = manifest()
    await signIn(page, 'reviewer')
    await page.goto(`/inquiries/${data.v11.inquiries.held}`)
    await expect(page.getByText('This is not an approval request')).toBeVisible()
    await expect(page.locator('#inquiry-status')).toContainText('LANGUAGE_UNRESOLVED')
    await expect(page.getByText('not resolved (never defaults to English)')).toBeVisible()
    await page.goto(`/inquiries/${data.v11.inquiries.uncertain}`)
    await expect(page.getByTestId('uncertain-notice')).toContainText('never resent blindly')
    await expect(page.getByText('submission uncertain').first()).toBeVisible()
    await page.goto(`/inquiries/${data.v11.inquiries.waiting}`)
    await expect(page.locator('#inquiry-status')).toContainText('RATE_CAP_REACHED')
    await expectNoApproveOrSendControl(page)
  })

  test('a viewer has no seller-inquiry area', async ({ page }) => {
    const { apiRequests } = guard(page)
    await signIn(page, 'viewer')
    await expect(page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Seller inquiries' })).toHaveCount(0)
    await page.goto('/inquiries')
    await expect(page.getByRole('heading', { name: 'Not available for your role' })).toBeVisible()
    await page.goto('/evaluation')
    await expect(page.getByRole('heading', { name: 'Not available for your role' })).toBeVisible()
    expect(apiRequests.filter((request) => /\/api\/(inquiries|replies|evaluation)/.test(request.url()))).toEqual([])
  })
})

test.describe('mail workers, lags and evaluation', () => {
  test('a powered-off PC is a coverage gap, never healthy', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await signIn(page, 'reviewer')
    await page.goto('/mail-workers')
    await expect(page.getByTestId('monitoring-summary')).toContainText('No mailbox is monitored right now')
    const card = page.getByTestId('mailbox-card')
    await expect(card).toHaveAttribute('data-monitoring', 'no')
    await expect(page.getByText('not monitoring: coverage gap')).toBeVisible()
    await expect(page.getByTestId('mailbox-not-monitoring')).toContainText('the PC may be off, asleep or offline')
    await expect(card).toContainText('6 h ago')
    // The server keeps the worker's LAST report (in sync, 10 s lag, empty backlog) after the PC went
    // off: none of it may read as current health.
    const reported = card.getByTestId('last-report')
    await expect(reported).toHaveCount(5)
    for (const text of await reported.allTextContents()) expect(text).toMatch(/^unknown now \(last report: /)
    await expect(reported.first()).toContainText('(last report: yes)')
    await expect(page.locator('section', { has: page.getByRole('heading', { name: data.v11.worker_label }) })).toBeVisible()
    const gap = page.getByTestId('coverage-gap').first()
    await expect(gap).toHaveAttribute('data-open', 'yes')
    await expect(gap).toContainText('worker offline')
    await expect(gap).toContainText('still open')
    await expect(page.getByText(/^monitoring$/)).toHaveCount(0)
    expect(problems).toEqual([])
  })

  test('lags: unknown is shown as unknown (never zero); detection delay needs a trustworthy source time', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await signIn(page, 'reviewer')
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Coverage & lags' }).click()
    await expect(page.getByRole('heading', { name: 'Coverage and lags', level: 1 })).toBeVisible()
    await expect(page.getByTestId('lag-notification_processing_lag')).toBeVisible()
    await expect(page.getByTestId('lag-mail_reply_detection_lag')).toBeVisible()
    const unknown = page.locator('[data-lag-status="unknown"]')
    expect(await unknown.count()).toBeGreaterThan(0)
    for (const text of await unknown.allTextContents()) {
      expect(text).toContain('unknown')
      expect(text).not.toMatch(/\b0 s\b/)
    }
    const measured = page.locator('[data-testid="lag-source_scan_lag"][data-lag-status="measured"]')
    await expect(measured.first()).toContainText('configured interval 15 min (context only, not a latency guarantee)')

    await page.goto(`/candidates/${data.v11.listings.replied}/lifecycle`)
    await expect(page.getByTestId('lag-detection_delay')).toContainText('unknown (no trustworthy source publication time)')
    await expect(page.getByText('not provided by the source')).toBeVisible()
    await expect(page.getByTestId('lag-detail_freshness')).toHaveAttribute('data-lag-status', 'measured')
    expect(problems).toEqual([])
  })

  test('15-day evaluation: zero suitable deals is zero and nothing is invented', async ({ page }) => {
    const { problems } = guard(page)
    await signIn(page, 'reviewer')
    await page.goto('/inquiries')
    await page.getByRole('navigation', { name: 'Seller inquiry area' }).getByRole('link', { name: '15-day evaluation' }).click()
    await expect(page.getByRole('heading', { name: '15-day evaluation', level: 1 })).toBeVisible()
    await expect(page.getByTestId('suitable-deals')).toHaveText('0')
    await expect(page.getByTestId('evaluation-outcome')).toContainText('Usable source coverage is not established yet')
    await expect(page.getByTestId('inquiries-sent')).toContainText('0')
    await expect(page.getByTestId('seller-replies')).toContainText('0')
    await expect(page.getByTestId('best-economics')).toContainText('unknown: no candidate has a complete valuation')
    await expect(page.getByTestId('evaluation-reasons')).not.toBeEmpty()
    expect(problems).toEqual([])
  })

  test('phone viewport: the v1.1 screens fit without horizontal scrolling', async ({ browser }) => {
    const data = manifest()
    const context = await browser.newContext({ viewport: { width: 375, height: 812 }, isMobile: true, hasTouch: true })
    const page = await context.newPage()
    const { problems } = guard(page)
    try {
      await signIn(page, 'owner')
      for (const [path, heading] of [
        ['/inquiries', 'Seller inquiries'],
        [`/inquiries/${data.v11.inquiries.replied}`, `Inquiry: ${data.v11.references.replied}`],
        [`/replies/${data.v11.replies.seller}`, `Reply: ${data.v11.references.replied}`],
        ['/inquiry-control', 'Inquiry control'],
        ['/mail-workers', 'Mail workers and coverage'],
        ['/lifecycle', 'Coverage and lags'],
        ['/evaluation', '15-day evaluation'],
      ] as const) {
        await page.goto(path)
        await expect(page.getByRole('heading', { name: new RegExp(`^${heading}`), level: 1 })).toBeVisible()
        await expectNoHorizontalScroll(page)
      }
      expect(problems).toEqual([])
    } finally {
      await context.close()
    }
  })
})

test.describe('inquiry control', () => {
  test.describe.configure({ mode: 'serial' })

  test('a reviewer sees the controls read-only: no pause, no resume', async ({ page }) => {
    await signIn(page, 'reviewer')
    await page.goto('/inquiry-control')
    await expect(page.getByTestId('control-approval')).toContainText('no per-message approval')
    await expect(page.getByText(/Your role cannot pause seller inquiries/)).toBeVisible()
    await expect(page.getByRole('button', { name: /Pause seller inquiries|Resume seller inquiries/ })).toHaveCount(0)
  })

  test('owner: pause survives a lost response through a same-key retry, then resume', async ({ page }) => {
    const { problems } = guard(page)
    const pauses: Array<{ key: string | undefined; body: string | null }> = []
    page.on('request', (request) => {
      if (request.url().endsWith('/api/inquiry-control/pause') && request.method() === 'POST') {
        pauses.push({ key: request.headers()['idempotency-key'], body: request.postData() })
      }
    })
    await signIn(page, 'owner')
    await page.goto('/inquiry-control')
    await expect(page.getByTestId('sending-state')).toContainText('These controls allow automatic inquiries')
    await page.getByLabel(/Pause reason/).fill('SYNTHETIC E2E: pausing seller inquiries for a check')
    let committed = 0
    await page.route(
      '**/api/inquiry-control/pause',
      async (route) => {
        const response = await route.fetch() // the server pauses...
        committed = response.status()
        await route.abort('internetdisconnected') // ...but the browser never sees the answer
      },
      { times: 1 },
    )
    await page.getByRole('button', { name: 'Pause seller inquiries' }).click()
    await expect(page.getByTestId('mutation-unconfirmed')).toContainText('Not confirmed: it may or may not have been saved')
    expect(committed).toBe(200)
    await expect(page.getByRole('button', { name: 'Pause seller inquiries' })).toBeDisabled()
    await page.getByRole('button', { name: 'Retry the same request' }).click()
    await expect(page.getByTestId('pause-confirmed')).toContainText('Seller inquiries paused.')
    await expect(page.locator('[data-testid="mutation-confirmed"]', { has: page.getByTestId('pause-confirmed') })).toContainText(
      'confirmed by the server on an idempotent retry',
    )
    expect(pauses).toHaveLength(2)
    expect(pauses[0]!.key).toBe(pauses[1]!.key)
    expect(pauses[0]!.body).toBe(pauses[1]!.body)
    await expect(page.getByTestId('sending-state')).toContainText('the inquiry kill switch is on')

    // The inquiry list reflects the pause; resume is a separate owner decision.
    await page.goto('/inquiries')
    await expect(page.getByTestId('control-summary')).toContainText('kill switch on')
    await page.goto('/inquiry-control')
    await page.getByLabel(/Resume reason/).fill('SYNTHETIC E2E: check finished')
    await page.getByRole('button', { name: 'Resume seller inquiries' }).click()
    await expect(page.getByTestId('resume-confirmed')).toContainText('Seller inquiries resumed')
    await expect(page.getByTestId('sending-state')).toContainText('These controls allow automatic inquiries')
    await expectNoApproveOrSendControl(page)
    expect(problems).toEqual([])
  })
})
