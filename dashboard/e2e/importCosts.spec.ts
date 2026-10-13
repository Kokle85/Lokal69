import { expect, test } from '@playwright/test'
import { expectNoHorizontalScroll, guard, manifest, signIn } from './helpers.ts'

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
    await page.getByLabel('Purchase price').fill('3000.00')
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
    const totals = page.getByTestId('totals-legal_base')
    await expect(totals).toContainText('Indicative total import charges (MKD)')
    await expect(totals).toContainText('MKD 168,154.45') // golden case G01 of the draft
    await expect(totals).toContainText('Indicative landed cost (EUR)')
    await expect(page.locator('tr.row-intermediate').first()).toContainText('not counted in the total')
    await expect(page).toHaveURL(/\/import-costs\?.*co2_nedc_g_km=189/)
    const calls = apiRequests.filter((request) => new URL(request.url()).pathname === '/api/import-costs/estimate')
    expect(calls.map((request) => request.method())).toEqual(['GET'])
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
    await expect(page.getByText('From the listing: EUR 2,750.00')).toBeVisible()
    const totals = page.getByTestId('totals-legal_base')
    await expect(totals).toContainText('unknown')
    await expect(totals).toContainText('Known subtotal (not a total)')
    await expect(totals).not.toContainText(/MKD 0\.00/)
    await expect(page.getByText('Needed to complete the calculation:')).toBeVisible()
    await expect(page.getByRole('table', { name: 'Facts used' })).toContainText('listing')
    await expectNoHorizontalScroll(page)
    expect(problems).toEqual([])
  })
})
