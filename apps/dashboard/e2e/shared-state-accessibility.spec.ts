import { expect, test } from '@playwright/test'

for (const path of ['/settings', '/admin/connections', '/admin/connections/conn-apex-load', '/apex-load', '/prompts/phase/story_analysis%2Fsystem']) {
  test(`cold route tabs retain real panel targets: ${path}`, async ({ page }) => {
    await page.goto(path)
    const tabs = page.getByRole('tablist').first()
    await expect(tabs).toBeVisible()
    const buttons = tabs.getByRole('tab')
    const missing = await buttons.evaluateAll(elements => elements.filter(tab => !document.getElementById(tab.getAttribute('aria-controls') ?? '')).map(tab => tab.textContent))
    expect(missing).toEqual([])
    await buttons.first().focus()
    await page.keyboard.press('End')
    await expect(buttons.last()).toBeFocused()
    const target = await buttons.last().getAttribute('aria-controls')
    await expect(page.locator(`[id="${target}"]`)).toBeVisible()
    await expect(buttons.last()).toHaveAttribute('aria-selected', 'true')
  })
}

test('small interactive controls have direct targets and disabled actions remain readable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 })
  await page.goto('/logs')
  const chips = page.locator('.level-chip')
  await expect(chips.first()).toBeVisible()
  for (const chip of await chips.all()) {
    const rect = await chip.boundingBox()
    expect(rect?.height).toBeGreaterThanOrEqual(24)
    expect(rect?.width).toBeGreaterThanOrEqual(24)
  }
  await page.goto('/runs')
  const title = page.locator('.runs-run-button').first()
  await expect(title).toBeVisible()
  expect((await title.boundingBox())?.height).toBeGreaterThanOrEqual(24)
  const disabled = page.getByRole('button', { name: 'Previous', exact: true })
  await expect(disabled).toBeDisabled()
  await expect(disabled).toHaveCSS('opacity', '1')
})

test('not-found and filtered states offer clear recovery without live-region noise', async ({ page }) => {
  await page.goto('/missing-route-for-state-check')
  const notFound = page.locator('[data-state="not-found"]')
  await expect(notFound).toBeVisible()
  await expect(notFound.getByRole('link', { name: 'Go to Home' })).toBeVisible()
  await page.goto('/prompts')
  await page.getByRole('searchbox', { name: 'Search prompts' }).fill('no-such-prompt-for-state-check')
  const filtered = page.locator('[data-state="filtered"]')
  await expect(filtered).toBeVisible()
  await expect(filtered).toHaveAttribute('role', 'region')
  await expect(filtered.getByRole('button', { name: /clear|show/i })).toBeVisible()
})
