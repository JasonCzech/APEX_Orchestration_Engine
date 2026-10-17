import { expect, test } from '@playwright/test'

for (const theme of ['dark', 'light', 'apex-light', 'solarized-dark', 'solarized-light', 'monokai-dimmed']) {
  test(`actual added and deleted prompt text retains contrast in ${theme}`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' })
    await page.addInitScript(name => localStorage.setItem('apex.theme', name), theme)
    for (const [version, baseline, selector] of [
      [2, 1, '.cm-changedLine'],
      [1, 2, '.cm-deletedChunk'],
    ] as const) {
      await page.goto(`/prompts/application/app-digital-device/versions/${version}?diff=prompt-app-device-v${baseline}`)
      const diff = page.getByRole('figure', { name: `Diff of v${version} against v${baseline}` })
      await expect(diff).toBeVisible()
      await diff.locator('.cm-scroller').evaluate(scroller => { scroller.scrollTop = scroller.scrollHeight })
      const changed = diff.locator(selector).filter({ hasText: 'Cite the supplied evidence and clearly label assumptions.' }).first()
      await expect(changed).toBeVisible()
      const measurements = await changed.evaluate(element => {
        const canvas = document.createElement('canvas')
        canvas.width = canvas.height = 1
        const context = canvas.getContext('2d')!
        // Canvas resolves both rgb() and modern color(srgb ...) from color-mix.
        const rgba = (value: string) => {
          context.clearRect(0, 0, 1, 1)
          context.fillStyle = value
          context.fillRect(0, 0, 1, 1)
          const values = [...context.getImageData(0, 0, 1, 1).data]
          return [...values.slice(0, 3), values[3]! / 255]
        }
        const composite = (ink: number[], base: number[]) => [
          ...ink.slice(0, 3).map((channel, index) => channel * ink[3]! + base[index]! * (1 - ink[3]!)), 1,
        ]
        const luminance = (color: number[]) => color.slice(0, 3).reduce((sum, channel, index) => {
          const value = channel / 255
          return sum + [0.2126, 0.7152, 0.0722][index]! * (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
        }, 0)
        const contrast = (target: Element) => {
          const ancestors: Element[] = []
          for (let node: Element | null = target; node; node = node.parentElement) ancestors.unshift(node)
          const background = ancestors.reduce((base, node) => composite(rgba(getComputedStyle(node).backgroundColor), base), [255, 255, 255, 1])
          const values = [luminance(background), luminance(composite(rgba(getComputedStyle(target).color), background))].sort((a, b) => a - b)
          return (values[1]! + 0.05) / (values[0]! + 0.05)
        }
        // Merge decorations can paint opaque backgrounds inside the line. Check
        // the parent of each actual text node, including nested syntax spans.
        const text: { content: string; ratio: number }[] = []
        const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
        for (let node = walker.nextNode(); node; node = walker.nextNode()) {
          if (node.textContent?.trim() && node.parentElement) {
            text.push({ content: node.textContent, ratio: contrast(node.parentElement) })
          }
        }
        return { surface: contrast(element), text }
      })
      const label = `${theme}, v${version} against v${baseline}`
      expect(measurements.surface, `${label}, outer changed-line surface`).toBeGreaterThanOrEqual(4.5)
      expect(measurements.text.length, `${label}, visible decorated text`).toBeGreaterThan(0)
      for (const { content, ratio } of measurements.text) {
        expect(ratio, `${label}, text ${JSON.stringify(content)}`).toBeGreaterThanOrEqual(4.5)
      }
      await test.info().attach(`${theme}-v${version}-contrast`, { body: JSON.stringify(measurements, null, 2), contentType: 'application/json' })
      if (version === 1) await expect(changed.locator('del').first()).toHaveCSS('text-decoration-line', 'line-through')
      else await expect(changed).toHaveCSS('border-inline-start-width', '3px')
    }
  })
}
