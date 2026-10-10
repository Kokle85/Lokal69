/**
 * Spec v1.1 screens against the REAL backend and the SYNTHETIC v1.1 world (tests/e2e/seed_v11.py):
 * seller inquiries, replies, inquiry control, mail-worker health, lags and the 15-day evaluation.
 * Nothing is ever sent: the E2E backend runs no worker and every address is `example.invalid`.
 */
import { expect, test, type Request } from '@playwright/test'
import { addKillSwitchSuppression, expectNoApproveOrSendControl, expectNoHorizontalScroll, guard, manifest, signIn } from './helpers.ts'

test.describe('seller inquiries and replies', () => {
  test('reviewer: attention groups by typed waiting reason, the caps wait and the list, with no approve or send control', async ({ page }) => {
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
    await expect(page.getByTestId('attention-suppressed')).toContainText(data.v11.references.killswitch)
    await expect(page.getByTestId('attention-suppressed')).toContainText('kill switch')
    // C3: the server's typed waiting reasons, each its own group (real plan jobs and worker state).
    const caps = page.getByTestId('attention-wait-RATE_CAP_REACHED')
    await expect(caps).toContainText(data.v11.references.waiting)
    await expect(caps).toContainText('The rolling caps are reached (2 of 2 in 24 h')
    await expect(caps.getByTestId('waiting-reason')).toHaveAttribute('data-reason', 'RATE_CAP_REACHED')
    const cooldown = page.getByTestId('attention-wait-SELLER_COOLDOWN')
    await expect(cooldown).toContainText(data.v11.references.cooldown)
    await expect(cooldown).toContainText('at least 7 days')
    const offline = page.getByTestId('attention-wait-WORKER_OFFLINE')
    await expect(offline).toContainText(data.v11.references.offline)
    await expect(offline).toContainText("classic Outlook on the owner's PC")
    // Each inquiry is listed once in the attention section (the offline one is waiting, not "stuck").
    await expect(page.getByTestId('attention-failed')).toHaveCount(0)
    const attention = page.locator('#attention')
    for (const key of ['offline', 'cooldown', 'uncertain', 'held', 'waiting', 'killswitch', 'suppressed'] as const) {
      await expect(attention.getByTestId('inquiry-row').filter({ hasText: data.v11.references[key] })).toHaveCount(1)
    }
    const list = page.getByRole('table', { name: 'Seller inquiries' })
    await expect(
      list.getByTestId('inquiry-row').filter({ hasText: data.v11.references.offline }).getByTestId('waiting-reason'),
    ).toHaveAttribute('data-reason', 'WORKER_OFFLINE')
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

  test('held, uncertain and waiting inquiries explain themselves without any approval wait', async ({ page }) => {
    const data = manifest()
    await signIn(page, 'reviewer')
    await page.goto(`/inquiries/${data.v11.inquiries.offline}`)
    await expect(page.getByTestId('waiting-notice')).toHaveAttribute('data-reason', 'WORKER_OFFLINE')
    await expect(page.getByTestId('waiting-notice')).toContainText('there is no approval to give and nothing to send by hand')
    await page.goto(`/inquiries/${data.v11.inquiries.cooldown}`)
    await expect(page.getByTestId('waiting-notice')).toHaveAttribute('data-reason', 'SELLER_COOLDOWN')
    await expect(page.locator('#inquiry-status').getByTestId('waiting-reason')).toContainText('seller cooldown')
    await page.goto(`/inquiries/${data.v11.inquiries.held}`)
    await expect(page.getByText('This is not an approval request')).toBeVisible()
    await expect(page.locator('#inquiry-status')).toContainText('LANGUAGE_UNRESOLVED')
    await expect(page.getByText('not resolved (never defaults to English)')).toBeVisible()
    await page.goto(`/inquiries/${data.v11.inquiries.uncertain}`)
    await expect(page.getByTestId('uncertain-notice')).toContainText('never resent blindly')
    await expect(page.getByText('submission uncertain').first()).toBeVisible()
    await page.goto(`/inquiries/${data.v11.inquiries.waiting}`)
    await expect(page.locator('#inquiry-status')).toContainText('RATE_CAP_REACHED')
    await expect(page.getByTestId('waiting-notice')).toHaveAttribute('data-reason', 'RATE_CAP_REACHED')
    await expectNoApproveOrSendControl(page)
  })

  test('replies show their dot-signal state (flood control), never message text in the list', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await signIn(page, 'reviewer')
    await page.goto('/replies')
    const rows = page.getByRole('table', { name: 'Seller replies' }).getByTestId('reply-row')
    await expect(rows).toHaveCount(2)
    await expect(rows.getByTestId('signal-status').first()).toBeVisible()
    await page.goto(`/replies/${data.v11.replies.seller}`)
    await expect(page.locator('#reply-message').getByTestId('signal-status')).toHaveAttribute('data-signal', 'emitted')
    await expect(page.getByTestId('signal-rate-limited')).toHaveCount(0)
    await expectNoApproveOrSendControl(page)
    expect(problems).toEqual([])
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
    // The worker's LAST report (in sync, 10 s lag, empty backlog) is stored, but without a healthy
    // heartbeat the server reports these worker-reported dimensions as UNKNOWN (work package C1,
    // item 8): none of it may read as current health.
    const reported = card.getByTestId('last-report')
    await expect(reported).toHaveCount(5)
    for (const text of await reported.allTextContents()) expect(text).toMatch(/^unknown now \(last report: unknown\)$/)
    await expect(page.locator('section', { has: page.getByRole('heading', { name: data.v11.worker_label }) })).toBeVisible()
    const gap = page.getByTestId('coverage-gap').first()
    await expect(gap).toHaveAttribute('data-open', 'yes')
    await expect(gap).toContainText('worker offline')
    await expect(gap).toContainText('still open')
    await expect(page.getByText(/^monitoring$/)).toHaveCount(0)
    expect(problems).toEqual([])
  })

  test('health: worker credentials, revoked workers, the reply-signal cap and the read-only activation evidence', async ({ page }) => {
    const data = manifest()
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'reviewer')
    await page.goto('/mail-workers')
    // The active worker's credential expires within the 14-day notice; the retired one is counted.
    const active = page.getByTestId('credential-row').filter({ hasText: data.v11.worker_label })
    await expect(active).toHaveAttribute('data-status', 'expiring')
    await expect(page.getByTestId('mailbox-card').getByTestId('credential-status')).toHaveAttribute('data-status', 'expiring')
    await expect(page.getByTestId('revoked-mailboxes')).toContainText('1 revoked mail worker(s) are not listed')
    await expect(page.getByTestId('credentials-not-live')).toHaveCount(0)
    const signals = page.getByTestId('reply-signals')
    await expect(signals.getByTestId('signal-cap')).toContainText('per 24 h PROPOSED')
    await expect(signals).toContainText('Signals emitted')
    // Activation evidence: what the API reports; the canary is never assumed and has no control.
    await expect(page.locator('[data-testid="activation-row"][data-evidence="sender"]')).toHaveAttribute('data-state', 'done')
    await expect(page.locator('[data-testid="activation-row"][data-evidence="authorization"]')).toHaveAttribute('data-state', 'done')
    await expect(page.locator('[data-testid="activation-row"][data-evidence="runtime"]')).toHaveAttribute('data-state', 'open')
    // D1: the canary evidence (rows 4-6) is the owner's; a reviewer never requests it.
    await expect(page.locator('[data-testid="activation-row"][data-evidence="canary"]')).toHaveAttribute('data-state', 'owner_only')
    await expect(page.getByTestId('canary-evidence')).toContainText('never assumed')
    expect(apiRequests.filter((request) => request.url().includes('/api/activation/canary-evidence'))).toEqual([])
    const activation = page.locator('#activation-evidence')
    await expect(activation.getByRole('button')).toHaveCount(0)
    await expect(activation.getByRole('link')).toHaveCount(0)

    // On request the revoked worker is listed, with its revoked credential.
    await page.getByLabel(/include revoked/).check()
    const retired = page.locator('section', { has: page.getByRole('heading', { name: data.v11.retired_worker_label }) })
    await expect(retired.getByTestId('mailbox-revoked')).toContainText('can never upload replies or claim sends again')
    await expect(page.getByTestId('credential-row').filter({ hasText: data.v11.retired_worker_label })).toHaveAttribute('data-status', 'revoked')
    await expect(page.locator('main')).not.toContainText('suvmail_')
    await expectNoApproveOrSendControl(page)
    // Only reads.
    expect(apiRequests.filter((request: Request) => request.method() !== 'GET')).toEqual([])
    expect(problems).toEqual([])
  })

  test('owner: the canary rows 4-6 show the evidence the server reports, read-only', async ({ page }) => {
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'owner')
    await page.goto('/mail-workers')
    // D1: GET /api/activation/canary-evidence (owner only). D2 (F3/OPS-04): nothing is reserved
    // without a completed canary of the current sender binding version, so the seed's owner ran
    // one (on the since-retired desktop worker): the evidence is "complete", one correlated canary.
    await expect(page.locator('[data-testid="activation-row"][data-evidence="canary"]')).toHaveAttribute('data-state', 'done')
    await expect(page.getByTestId('canary-evidence')).toHaveAttribute('data-evidence-state', 'complete')
    await expect(page.getByTestId('canary-evidence')).toContainText('outlook local sender binding')
    await expect(page.getByTestId('canary-item')).toHaveCount(1)
    await expect(page.getByTestId('canary-item').first()).toHaveAttribute('data-state', 'reply_correlated')
    const activation = page.locator('#activation-evidence')
    await expect(activation).toContainText('sent only by the owner on the command line')
    await expect(activation.getByRole('button')).toHaveCount(0)
    await expect(activation.getByRole('link')).toHaveCount(0)
    expect(apiRequests.filter((request) => request.url().includes('/api/activation/canary-evidence')).length).toBeGreaterThan(0)
    expect(apiRequests.filter((request: Request) => request.method() !== 'GET')).toEqual([])
    await expect(page.locator('main')).not.toContainText('@example.invalid')
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
    // C3: the readiness of the standing authorization and of the CONFIGURED sending identity.
    await expect(page.getByTestId('authorization-status')).toHaveAttribute('data-status', 'active')
    await expect(page.getByTestId('authorization-status')).toContainText('version 1')
    await expect(page.getByTestId('sender-readiness')).toHaveAttribute('data-readiness', 'ready')
    await expect(page.getByTestId('sender-readiness')).toContainText('outlook local')
    // D1: the E2E backend runs with SELLER_INQUIRY_MODE at its default: the database controls say
    // automatic, but the process gate is closed, so the screen never claims anything can be sent.
    await expect(page.getByTestId('sending-state')).toContainText('nothing can be sent now')
    await expect(page.getByTestId('sending-state')).toContainText('SELLER_INQUIRY_MODE is disabled until sender ready, not automatic')
    await expect(page.getByTestId('sending-state')).not.toContainText('the backend process gate is open')
    await expect(page.getByTestId('process-gate')).toHaveAttribute('data-open', 'no')
    await expect(page.getByTestId('automatic-possible')).toHaveAttribute('data-possible', 'no')
    await expect(page.locator('main')).not.toContainText('@example.invalid')
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
    // Every untransmitted inquiry now waits for the owner's resume (typed by the server).
    const paused = page.getByTestId('attention-wait-INQUIRIES_PAUSED')
    await expect(paused).toContainText(manifest().v11.references.waiting)
    await expect(paused).toContainText(manifest().v11.references.cooldown)
    await expect(page.getByTestId('attention-wait-RATE_CAP_REACHED')).toHaveCount(0)
    await page.goto('/inquiry-control')
    await page.getByLabel(/Resume reason/).fill('SYNTHETIC E2E: check finished')
    await page.getByRole('button', { name: 'Resume seller inquiries' }).click()
    await expect(page.getByTestId('resume-confirmed')).toContainText('Seller inquiries resumed')
    await expect(page.getByTestId('sending-state')).toContainText('These controls allow automatic inquiries')
    await expectNoApproveOrSendControl(page)
    expect(problems).toEqual([])
  })

  test('owner: a resume removes only the suppressions the owner confirmed; a changed count is refused and shown first', async ({ page }) => {
    const { problems } = guard(page)
    const resumes: Array<{ key: string | undefined; body: Record<string, unknown> }> = []
    page.on('request', (request) => {
      if (request.url().endsWith('/api/inquiry-control/resume') && request.method() === 'POST') {
        resumes.push({ key: request.headers()['idempotency-key'], body: JSON.parse(request.postData() ?? '{}') as Record<string, unknown> })
      }
    })
    await signIn(page, 'owner')
    await page.goto('/inquiry-control')
    // Kill switch off, but kill-switch suppressions are still active: the owner can re-qualify.
    const form = page.getByTestId('resume-form')
    await expect(form).toBeVisible()
    const box = form.getByRole('checkbox', { name: /Also remove the \d+ kill-switch/ })
    const shown = Number(/Also remove the (\d+)/.exec((await form.locator('label.checkbox').textContent()) ?? '')?.[1])
    expect(shown).toBeGreaterThan(0)
    await form.getByLabel(/Resume reason/).fill('SYNTHETIC E2E: re-qualify what the kill switch stopped')
    await box.check()

    // Meanwhile one more kill-switch suppression is recorded on the server (real repository).
    expect(addKillSwitchSuppression()).toBe(shown + 1)

    await page.getByRole('button', { name: 'Re-qualify suppressed inquiries' }).click()
    const rejected = page.getByTestId('mutation-rejected')
    await expect(rejected).toContainText('The suppressions a resume would remove changed')
    await expect(rejected).toContainText(`You confirmed removing ${shown} suppression(s), but the server now counts ${shown + 1}`)
    await expect(rejected.getByTestId('error-reason')).toHaveText('suppressions_changed')
    // The controls were reloaded: the NEW count is shown and must be confirmed before anything else.
    await expect(page.getByTestId('suppressions-changed')).toContainText(`you confirmed ${shown}, the current count is ${shown + 1}`)
    await expect(page.getByTestId('removal-count-moved')).toBeVisible()
    await expect(page.getByRole('button', { name: 'Re-qualify suppressed inquiries' })).toBeDisabled()
    await expect(form.locator('label.checkbox')).toContainText(`Also remove the ${shown + 1} kill-switch`)
    expect(resumes).toHaveLength(1)

    await box.uncheck()
    await box.check()
    await page.getByRole('button', { name: 'Re-qualify suppressed inquiries' }).click()
    await expect(page.getByTestId('resume-confirmed')).toContainText(`${shown + 1} suppression(s) removed, each audited.`)
    expect(resumes).toHaveLength(2)
    expect(resumes[0]!.body).toMatchObject({ remove_suppressions: true, expected_removable_suppressions: shown })
    expect(resumes[1]!.body).toMatchObject({ remove_suppressions: true, expected_removable_suppressions: shown + 1 })
    expect(resumes[0]!.key).not.toBe(resumes[1]!.key)
    // Nothing left to remove and the kill switch is off: no resume action remains.
    await expect(page.getByTestId('resume-form')).toHaveCount(0)
    await expectNoApproveOrSendControl(page)
    expect(problems).toEqual([])
  })
})
