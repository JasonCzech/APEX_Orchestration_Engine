import { expect, test } from '@playwright/test'

const themes = ['dark', 'light', 'solarized-dark', 'solarized-light', 'monokai-dimmed']

for (const theme of themes) {
  test(`tabs use Load underlines and retain keyboard focus: ${theme}`, async ({ page }) => {
    await page.addInitScript(name => localStorage.setItem('apex.theme', name), theme)
    await page.goto('/settings')
    const tabs = page.getByRole('tablist', { name: 'Settings sections' })
    const first = tabs.getByRole('tab').first()
    await expect(first).toHaveAttribute('aria-selected', 'true')
    await expect(first).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
    await expect(first).toHaveCSS('border-bottom-width', '0px')
    const selected = await first.evaluate(element => {
      const style = getComputedStyle(element)
      const probe = document.createElement('span')
      probe.style.color = 'var(--text-primary)'
      element.append(probe)
      const primary = getComputedStyle(probe).color
      probe.remove()
      return { color: style.color, primary, shadow: style.boxShadow }
    })
    expect(selected.color).toBe(selected.primary)
    expect(selected.shadow).toMatch(/-3px/)
    expect(selected.shadow).toContain('inset')
    await first.hover()
    await expect(first).toHaveCSS('box-shadow', selected.shadow)
    await expect(first).toHaveCSS('color', selected.primary)
    const firstHeight = (await first.boundingBox())!.height
    await first.focus()
    await page.keyboard.press('ArrowRight')
    const second = tabs.getByRole('tab').nth(1)
    await expect(second).toBeFocused()
    await expect(second).toHaveAttribute('aria-selected', 'true')
    await expect(second).toHaveCSS('outline-style', 'solid')
    await expect(second).toHaveCSS('outline-width', '2px')
    expect((await first.boundingBox())!.height).toBe(firstHeight)
    const panelId = await second.getAttribute('aria-controls')
    await expect(page.locator(`[id="${panelId}"]`)).toBeVisible()
    expect(await tabs.locator('[role="tab"][tabindex="0"]').count()).toBe(1)
  })

  test(`shared surfaces on a cold page load: ${theme}`, async ({ page }) => {
    await page.addInitScript((name) => {
      localStorage.setItem('apex.theme', name)
      localStorage.setItem('apex.preferences.v1', JSON.stringify({ reduceMotion: true }))
    }, theme)
    await page.goto('/prompts')
    await expect(page.getByRole('heading', { name: 'Prompts', exact: true })).toBeVisible()
    await expect(page.locator('html')).toHaveAttribute('data-theme', theme)
    await expect(page.locator('html')).toHaveAttribute('data-reduce-motion', 'true')
    const surfaces = await page.evaluate(() => {
      // Probe the shared notice before the Run Detail route has ever loaded.
      const notice = document.createElement('div')
      notice.className = 'tonal-card danger'
      document.body.append(notice)
      const style = getComputedStyle(notice)
      const result = { padding: style.paddingTop, border: style.borderTopWidth, background: style.backgroundColor,
        scheme: getComputedStyle(document.documentElement).colorScheme }
      notice.remove()
      return result
    })
    expect(surfaces.padding).toBe('8px')
    expect(surfaces.border).toBe('1px')
    expect(surfaces.background).not.toBe('rgba(0, 0, 0, 0)')
    expect(surfaces.scheme).toBe(theme.endsWith('light') ? 'light' : 'dark')
    await page.goto('/runs/new')
    await expect(page.getByLabel('Title', { exact: true })).toBeVisible()
    const form = await page.getByLabel('Title', { exact: true }).evaluate((input) => {
      const style = getComputedStyle(input)
      return { background: style.backgroundColor, color: style.color }
    })
    expect(form.background).not.toBe('rgba(0, 0, 0, 0)')
    expect(form.color).not.toBe(form.background)
  })
}

test('retired theme migrates before config loads and remains migrated after bootstrap', async ({ page }) => {
  let finishConfig: (() => void) | undefined
  await page.addInitScript(() => {
    localStorage.setItem('apex.theme', 'apex-light')
    localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}')
  })
  await page.route('**/config.json', async (route) => {
    await new Promise<void>((resolve) => { finishConfig = resolve })
    await route.fulfill({ json: { dataMode: 'demo', apexOrigin: '', langgraphOrigin: '' } })
  })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.locator('html')).toHaveAttribute('data-reduce-motion', 'true')
  expect(await page.evaluate(() => localStorage.getItem('apex.theme'))).toBe('light')
  await expect.poll(() => Boolean(finishConfig)).toBe(true)
  finishConfig?.()
  await expect(page.getByRole('button', { name: 'Account options for Demo Presenter' })).toBeVisible()
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
})

for (const width of [390, 768, 1440]) {
  test(`shared typography reflows at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 })
    await page.goto('/prompts')
    await expect(page.getByRole('heading', { name: 'Prompts', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    await page.goto('/runs/new')
    await expect(page.getByLabel('Title', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
