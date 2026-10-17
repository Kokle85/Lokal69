import { expect, test } from '@playwright/test'
import { expectNoHorizontalScroll, guard, manifest, signIn } from './helpers.ts'

/** "Purchase price (real, for profit) · Реална набавна цена (за профит)" (owner decision 2026-10-13). */
const PRICE_LABEL = /^Purchase price \(real, for profit\)/

/**
 * The read-only customs calculator against the real backend: the shipped DRAFT rule set (never
 * approved or activated here) gives a labelled INDICATIVE PREVIEW. The seed records no customs
 * rate and no listing has a CO2 figure, so a listing-based estimate stays incomplete (unknown,
 * never zero); the vehicle-facts estimate passes the golden-case customs rate (a SYNTHETIC test
 * input, used for that request only and never stored).
 */
test.describe('customs calculator', () => {
  test('estimates from vehicle facts as an indicative preview labelled in Macedonian and English', async ({ page }) => {
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'viewer')
    await page.getByRole('navigation', { name: 'Primary' }).getByRole('link', { name: 'Customs calculator' }).click()
    await expect(page.getByRole('heading', { name: 'Customs calculator', level: 1 })).toBeVisible()
    await page.getByLabel(PRICE_LABEL).fill('3000.00')
    await page.getByLabel('Price currency').selectOption('EUR')
    await page.getByLabel('Price basis').selectOption('invoice')
    await page.getByLabel('Fuel / drive').selectOption('diesel')
    await page.getByLabel('CO2 NEDC (g/km)').fill('189')
    await page.getByLabel('CO2 document').selectOption('coc')
    await page.getByLabel('Customs declaration date').fill('2026-10-13')
    await page.getByLabel('Customs rate EUR/MKD').fill('61.5337')
    await page.getByRole('button', { name: 'Calculate' }).click()

    const banner = page.getByTestId('estimate-banner')
    await expect(banner).toContainText('ИНДИКАТИВНА ПРОЦЕНКА')
    await expect(banner).toContainText('INDICATIVE PREVIEW')
    await expect(banner.locator('p[lang="mk"]')).toContainText('неодобрени нацрт-правила')
    const totals = page.getByTestId('totals-primary')
    await expect(totals).toContainText('Indicative total import charges (MKD)')
    await expect(totals).toContainText('MKD 168,155') // golden case G01 of the draft (whole denars)
    await expect(totals).toContainText('Indicative landed cost (EUR)')
    await expect(page.locator('tr.row-intermediate').first()).toContainText('not counted in the total')
    // The two values side by side (no estimate given: the duties are on the price paid), whole euros.
    await expect(page.getByTestId('value-purchase-price')).toContainText('EUR 3,000')
    await expect(page.getByTestId('value-customs-value')).toContainText('EUR 3,700')
    await expect(page.getByTestId('glance-transaction_value')).toHaveCount(0)
    await expect(page).toHaveURL(/\/import-costs\?.*co2_nedc_g_km=189/)
    const calls = apiRequests.filter((request) => new URL(request.url()).pathname === '/api/import-costs/estimate')
    expect(calls.map((request) => request.method())).toEqual(['GET'])
    expect(problems).toEqual([])
  })

  test("computes the owner's example: duties on the estimated customs value, profit on the price paid", async ({ page }) => {
    // Owner decision 2026-10-13: bought for EUR 2,800 while the SchwackeNet catalogue says EUR 4,200.
    const { problems } = guard(page)
    await signIn(page, 'viewer')
    await page.goto('/import-costs')
    await page.getByLabel(PRICE_LABEL).fill('2800')
    await page.getByLabel('Price currency').selectOption('EUR')
    await page.getByLabel('Price basis').selectOption('invoice')
    await page.getByLabel(/^Estimated customs value \(for duties\)/).fill('4200')
    await page.getByLabel(/^Source of the estimated customs value/).fill('SchwackeNet catalogue')
    await page.getByLabel('Fuel / drive').selectOption('diesel')
    await page.getByLabel('CO2 NEDC (g/km)').fill('189')
    await page.getByLabel('CO2 document').selectOption('coc')
    await page.getByLabel('Customs declaration date').fill('2026-10-13')
    await page.getByLabel('Customs rate EUR/MKD').fill('61.5337')
    await page.getByRole('button', { name: 'Calculate' }).click()

    await expect(page.getByTestId('estimate-banner')).toContainText('INDICATIVE PREVIEW')
    await expect(page.getByTestId('value-purchase-price')).toContainText('EUR 2,800')
    const customs = page.getByTestId('value-customs-value')
    await expect(customs).toContainText('EUR 4,900') // 4,200 + the fixed EUR 700 transport to the border
    await expect(customs).toContainText('Source: SchwackeNet catalogue')
    const primary = page.getByTestId('glance-primary')
    await expect(primary).toContainText('MKD 185,803')
    await expect(primary).toContainText('EUR 6,520') // landed cost on the price actually paid
    const alternative = page.getByTestId('glance-transaction_value')
    await expect(alternative).toContainText('EUR 3,500')
    await expect(alternative).toContainText('MKD 165,213')
    await expect(alternative).toContainText('EUR 6,185')
    const amounts = await page.getByTestId('estimate').locator('.amount').allTextContents()
    expect(amounts.length).toBeGreaterThan(20)
    expect(amounts.filter((text) => /\d\.\d/.test(text))).toEqual([]) // whole units only
    expect(problems).toEqual([])
  })

  test('starts from a listing and keeps unknown charges unknown, never zero', async ({ page }) => {
    const data = manifest()
    const { problems } = guard(page)
    await page.setViewportSize({ width: 360, height: 800 })
    await signIn(page, 'reviewer')
    await page.goto(`/candidates/${data.listings.alpha}`)
    await page.getByRole('navigation', { name: 'Related views' }).getByRole('link', { name: 'Customs calculator' }).click()
    await expect(page).toHaveURL(new RegExp(`/candidates/${data.listings.alpha}/import-costs$`))
    await expect(page.getByTestId('estimate-banner')).toContainText('INDICATIVE PREVIEW')
    await expect(page.getByText('From the listing: EUR 2,750', { exact: true })).toBeVisible() // whole euros
    const totals = page.getByTestId('totals-primary')
    await expect(totals).toContainText('unknown')
    await expect(totals).toContainText('Known subtotal (not a total)')
    await expect(totals).not.toContainText(/MKD 0\b/)
    await expect(page.getByText('Needed to complete the calculation:')).toBeVisible()
    await expect(page.getByRole('table', { name: 'Facts used' })).toContainText('listing')
    await expectNoHorizontalScroll(page)
    expect(problems).toEqual([])
  })
})
