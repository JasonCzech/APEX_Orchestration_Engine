import { expect, test } from '@playwright/test'

test('wizard catalog prompts and overrides are keyboard reachable without changing content', async ({ page }) => {
  await page.goto('/runs/new?step=prompts')
  // The prompt rail is one tab stop; arrows move between slots.
  const rail = page.getByRole('tablist', { name: 'Prompt selection' })
  const system = rail.getByRole('tab').first()
  await expect(system).toHaveAttribute('aria-selected', 'true')
  await expect(rail.locator('[role="tab"][tabindex="0"]')).toHaveCount(1)
  await system.focus()
  await page.keyboard.press('ArrowDown')
  await expect(rail.getByRole('tab').nth(1)).toBeFocused()
  await page.keyboard.press('ArrowUp')
  await expect(system).toBeFocused()

  // Catalog content is shown read-only until it is overridden for this test.
  const panel = page.locator(`[id="${await system.getAttribute('aria-controls')}"]`)
  const reader = panel.locator('pre.wizard-prompt-content')
  await expect(reader).toBeVisible()
  await expect(reader).not.toHaveAttribute('contenteditable', /.*/)
  const content = await reader.innerText()
  expect(content.length).toBeGreaterThan(0)
  // The whole prompt is shown; no inner scroll region hides text from keyboard users.
  expect(await reader.evaluate(element => element.scrollHeight <= element.clientHeight + 1)).toBe(true)

  const override = panel.getByRole('button', { name: 'Override for this test' })
  await override.click()
  const editor = panel.locator('.cm-content')
  await expect(editor).toHaveAttribute('contenteditable', 'true')
  await expect(editor).toHaveAttribute('tabindex', '0')
  await expect(editor).toHaveAttribute('aria-label', /^Override /)
  // The editor starts from the catalog text. CodeMirror renders blank lines as
  // their own line elements, so compare the words rather than raw newlines.
  const words = (text: string) => text.replace(/\s+/g, ' ').trim()
  const original = await editor.innerText()
  expect(words(original)).toBe(words(content))
  await editor.focus()
  const cursor = panel.locator('.cm-cursor').first()
  await expect(cursor).toBeVisible()
  await expect(cursor).toHaveCSS('border-left-color', await editor.evaluate(element => getComputedStyle(element).color))
  // Tab leaves the editor instead of inserting a tab character.
  await page.keyboard.press('Tab')
  await expect(editor).not.toBeFocused()
  expect(await editor.innerText()).toBe(original)
})

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
