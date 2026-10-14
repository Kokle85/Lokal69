import { expect, test } from '@playwright/test'
import { expectNoHorizontalScroll, guard, manifest, signIn } from './helpers.ts'

/**
 * A listing's recorded import values against the real backend (owner decision 2026-10-13: the real
 * purchase price and the estimated customs value are two separate values per vehicle). The owner
 * records them on the `paused` listing, which no other spec reads (recording marks the listing's
 * valuations stale and queues a recomputation); every value is SYNTHETIC.
 */
test.describe('import values', () => {
  test('the owner records both values and the calculator uses them for that listing', async ({ page }) => {
    const data = manifest()
    const { problems, apiRequests } = guard(page)
    await signIn(page, 'owner')
    await page.goto(`/candidates/${data.listings.paused}`)
    const panel = page.getByRole('region', { name: 'Import values' })
    await expect(panel.getByTestId('recorded-purchase-price-none')).toContainText('Not recorded')
    const form = panel.getByTestId('import-values-form')
    await form.getByLabel('Purchase price (EUR)').fill('2800.50')
    await form.getByLabel('Kind').selectOption('actual')
    await form.getByLabel('Source of the purchase price').fill('SYNTHETIC seller invoice')
    await form.getByLabel('Estimated customs value (EUR, without transport)').fill('4200')
    await form.getByLabel('Source of the estimated customs value').fill('SchwackeNet catalogue (SYNTHETIC)')
    await expect(form.getByTestId('import-values-effect')).toContainText("Recording changes this listing's valuation")
    await form.getByLabel(/I understand that this changes this listing's valuation/).check()
    await form.getByRole('button', { name: 'Record values' }).click()

    await expect(form.getByTestId('import-values-recorded')).toContainText('Recorded 2 values')
    await expect(panel.getByTestId('recorded-purchase-price')).toContainText('EUR 2,801') // whole euros
    await expect(panel.getByTestId('recorded-purchase-price')).toContainText('Kind: actual (paid)')
    await expect(panel.getByTestId('recorded-customs-value')).toContainText('EUR 4,200')
    const posts = apiRequests.filter((request) => request.method() === 'POST')
    expect(posts.map((request) => new URL(request.url()).pathname)).toEqual([`/api/listings/${data.listings.paused}/import-values`])

    // The calculator of this listing uses the recorded values (labelled as recorded).
    await panel.getByTestId('import-values-calculator-link').click()
    await expect(page).toHaveURL(new RegExp(`/candidates/${data.listings.paused}/import-costs$`))
    await expect(page.getByText('Recorded for this listing: EUR 2,801', { exact: true })).toBeVisible()
    const price = page.getByTestId('value-purchase-price')
    await expect(price).toContainText('EUR 2,801')
    await expect(price).toContainText('recorded by the owner: actual (paid)')
    const customs = page.getByTestId('value-customs-value')
    await expect(customs).toContainText('EUR 4,900') // the estimate + the fixed EUR 700 transport
    await expect(customs).toContainText('estimated customs value + transport to the MK border')
    await expect(page.getByTestId('glance-transaction_value')).toBeVisible()
    await expect(page.getByRole('table', { name: 'Facts used' })).toContainText('recorded value')
    expect(problems).toEqual([])
  })

  test('viewers read the values but cannot record them', async ({ page }) => {
    const data = manifest()
    const { problems, apiRequests } = guard(page)
    await page.setViewportSize({ width: 360, height: 800 })
    await signIn(page, 'viewer')
    await page.goto(`/candidates/${data.listings.alpha}`)
    const panel = page.getByRole('region', { name: 'Import values' })
    await expect(panel.getByTestId('import-values-current')).toBeVisible()
    await expect(panel.getByTestId('import-values-owner-only')).toContainText('Only the workspace owner')
    await expect(panel.getByTestId('import-values-form')).toHaveCount(0)
    await expect(panel.getByTestId('import-values-calculator-link')).toHaveAttribute(
      'href',
      `/candidates/${data.listings.alpha}/import-costs`,
    )
    await expectNoHorizontalScroll(page)
    expect(apiRequests.filter((request) => request.method() !== 'GET')).toEqual([])
    expect(problems).toEqual([])
  })
})
