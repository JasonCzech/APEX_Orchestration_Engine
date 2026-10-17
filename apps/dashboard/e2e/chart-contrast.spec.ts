import { expect, test } from '@playwright/test'

for (const theme of ['dark', 'light', 'apex-light', 'solarized-dark', 'solarized-light', 'monokai-dimmed']) {
  test(`chart strokes contrast with the rendered panel and retain non-color encodings: ${theme}`, async ({ page }) => {
    await page.addInitScript(name => localStorage.setItem('apex.theme', name), theme)
    await page.goto('/analytics')
    const chart = page.getByTestId('analytics-agent-series-chart')
    await expect(chart).toBeVisible()
    await expect(chart.locator('.recharts-area-curve').first()).toBeVisible()
    const ratios = await chart.evaluate(panel => {
      const parse = (value: string) => {
        const channels = value.match(/[\d.]+/g)!.map(Number)
        return [...channels.slice(0, 3), channels[3] ?? 1]
      }
      const composite = (ink: number[], base: number[]) => [
        ...ink.slice(0, 3).map((channel, index) => channel * ink[3] + base[index] * (1 - ink[3])), 1,
      ]
      const luminance = (color: number[]) => color.slice(0, 3).reduce((sum, channel, index) => {
        const value = channel / 255
        return sum + [0.2126, 0.7152, 0.0722][index] * (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
      }, 0)
      const ancestors: Element[] = []
      for (let node: Element | null = panel; node; node = node.parentElement) ancestors.unshift(node)
      const background = ancestors.reduce((base, node) => composite(parse(getComputedStyle(node).backgroundColor), base), [255, 255, 255, 1])
      const probe = document.createElement('span')
      panel.append(probe)
      try {
        return Array.from({ length: 8 }, (_, index) => {
          probe.style.color = `var(--chart-${index + 1})`
          const ink = composite(parse(getComputedStyle(probe).color), background)
          const values = [luminance(ink), luminance(background)].sort((a, b) => a - b)
          return { token: `--chart-${index + 1}`, ratio: (values[1] + 0.05) / (values[0] + 0.05) }
        })
      } finally {
        probe.remove()
      }
    })
    for (const { token, ratio } of ratios) expect(ratio, `${theme} ${token}`).toBeGreaterThanOrEqual(3)
    expect(await chart.locator('pattern').count()).toBeGreaterThan(1)
    const dashes = await chart.locator('.recharts-area-curve').evaluateAll(curves => curves.map(curve => getComputedStyle(curve).strokeDasharray))
    expect(new Set(dashes).size).toBeGreaterThan(1)
  })
}
