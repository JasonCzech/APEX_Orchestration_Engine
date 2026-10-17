import { expect, test } from '@playwright/test'

const themes = ['dark', 'light', 'apex-light', 'solarized-dark', 'solarized-light', 'monokai-dimmed']

for (const theme of themes) {
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

test('theme and motion preferences apply while config is still pending', async ({ page }) => {
  let finishConfig: (() => void) | undefined
  await page.addInitScript(() => {
    localStorage.setItem('apex.theme', 'light')
    localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}')
  })
  await page.route('**/config.json', async (route) => {
    await new Promise<void>((resolve) => { finishConfig = resolve })
    await route.fulfill({ json: { dataMode: 'demo', apexOrigin: '', langgraphOrigin: '' } })
  })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
  await expect(page.locator('html')).toHaveAttribute('data-reduce-motion', 'true')
  await expect.poll(() => Boolean(finishConfig)).toBe(true)
  finishConfig?.()
  await expect(page.getByRole('button', { name: 'Account options for Demo Presenter' })).toBeVisible()
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
