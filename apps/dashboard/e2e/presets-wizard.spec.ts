import { expect, test, type Page } from '@playwright/test'

/** Presets × wizard in the offline demo: Start offers the primary application's
 * presets (F-01, F-16), the preset bar tracks changes (F-05), Review saves the
 * test as a preset (F-02), and the presets list groups by application (F-15). */
test.setTimeout(120_000)
test.use({ actionTimeout: 10_000 })

async function resetDemo(page: Page) {
  await page.goto('/')
  await page.evaluate(() => localStorage.removeItem('apex.demo.state.v1'))
  await page.reload()
  await expect(page.getByText('DEMO · OFFLINE')).toBeVisible()
}

test('start from a preset for the primary application, change it, and save it as a new preset', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 })
  await resetDemo(page)
  await page.goto('/runs/new')
  await expect(page.getByRole('heading', { name: 'Start a new test', exact: true })).toBeVisible()
  await page.getByRole('combobox', { name: 'Project' }).selectOption({ label: 'ACC Perf' })
  await page.getByRole('combobox', { name: 'Application' }).selectOption({ label: 'Mule' })

  const forMule = page.getByRole('region', { name: 'For Mule' })
  await expect(forMule.getByText('Mule API soak — 4 h')).toBeVisible()
  // Presets for other applications stay behind a toggle.
  await expect(page.getByText('ACC quick regression')).toHaveCount(0)
  await forMule.getByRole('button', { name: 'Use preset: Mule API soak — 4 h' }).click()

  const bar = page.getByTestId('preset-bar')
  await expect(bar).toContainText('Mule API soak — 4 h')
  await expect(bar).toContainText('no changes')
  // The wizard opens at its top, not at Start's scroll position.
  expect(await page.locator('#main-content').evaluate(element => element.scrollTop)).toBe(0)
  await expect(page.getByRole('group', { name: 'Backend · Primary system' }).getByRole('combobox', { name: 'Application' })).toHaveValue(/mule/)

  await page.getByRole('tab', { name: 'Config', exact: true }).click()
  await page.getByRole('radio', { name: 'Review every phase' }).click()
  await expect(bar).toContainText('1 change')

  await page.getByRole('tab', { name: 'Review', exact: true }).click()
  await page.getByRole('button', { name: 'Save as preset…' }).click()
  const dialog = page.getByRole('dialog', { name: 'Save as preset' })
  await dialog.getByRole('radio', { name: /new preset/i }).check()
  await dialog.getByRole('textbox', { name: 'Name' }).fill('Mule soak with reviews')
  await dialog.getByRole('button', { name: 'Save preset' }).click()
  await expect(dialog).toHaveCount(0)
  await expect(bar).toContainText('Mule soak with reviews')
  await expect(bar).toContainText('no changes')

  await page.goto('/test-presets?application=app-acc-perf-mule')
  const group = page.getByRole('region', { name: 'Presets for Mule' })
  await expect(group.getByRole('link', { name: 'Mule soak with reviews' })).toBeVisible()
  await expect(group.getByRole('link', { name: 'Mule API soak — 4 h' })).toBeVisible()
  await group.getByRole('link', { name: 'Mule soak with reviews' }).click()
  await expect(page.getByLabel('Preset settings')).toContainText('Review every phase')
  await expect(page.getByRole('link', { name: 'Start a test from this preset' })).toBeVisible()
  await resetDemo(page)
})

for (const width of [390, 768]) {
  test(`Start and the presets list fit at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/runs/new')
    await expect(page.getByRole('heading', { name: 'Start a new test', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.goto('/test-presets')
    await expect(page.getByRole('region', { name: 'Presets for Mule' })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
