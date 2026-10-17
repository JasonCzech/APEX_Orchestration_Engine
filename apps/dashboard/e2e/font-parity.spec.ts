import { readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const sans = "'ATT Aleck Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"
const mono = "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', monospace"

const fontFamilies = (stack: string) => stack.split(',').map(name => name.trim().replace(/^['"]|['"]$/g, ''))

for (const theme of ['dark', 'light', 'apex-light', 'solarized-dark', 'solarized-light', 'monokai-dimmed']) {
  test(`shared brand face loads locally with system mono in ${theme}`, async ({ page }) => {
    const fontRequests: string[] = []
    page.on('request', request => { if (request.resourceType() === 'font') fontRequests.push(request.url()) })
    await page.addInitScript(name => localStorage.setItem('apex.theme', name), theme)
    await page.goto('/settings')
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
    await expect(page.getByRole('heading', { level: 1 })).toHaveCSS('font-size', '19px')
    await expect(page.getByRole('heading', { level: 1 })).toHaveCSS('font-weight', '850')
    const result = await page.evaluate(async () => {
      const faces = (await Promise.all([400, 500, 700, 900].map(weight => document.fonts.load(`${weight} 16px "ATT Aleck Sans"`)))).flat()
      const root = getComputedStyle(document.documentElement)
      return {
        sans: root.getPropertyValue('--font-sans').trim(),
        mono: root.getPropertyValue('--font-mono').trim(),
        body: getComputedStyle(document.body).fontFamily,
        faces: faces.map(face => ({ family: face.family, weight: face.weight, status: face.status })),
      }
    })
    expect(fontFamilies(result.sans)).toEqual(fontFamilies(sans))
    expect(fontFamilies(result.mono)).toEqual(fontFamilies(mono))
    expect(result.body.startsWith('"ATT Aleck Sans"')).toBe(true)
    expect(result.faces.map(face => [face.weight, face.status])).toEqual(['400', '500', '700', '900'].map(weight => [weight, 'loaded']))
    expect(fontRequests).toHaveLength(4)
    for (const url of fontRequests) {
      expect(new URL(url).origin).toBe(new URL(page.url()).origin)
      expect(new URL(url).pathname).toMatch(/^\/fonts\/att\/ATTAleckSans_W_(Rg|Md|Bd|Blk)\.woff2$/)
    }
    await page.goto('/prompts/application/app-digital-device')
    const editor = page.locator('.code-viewer .cm-scroller').first()
    await expect(editor).toBeVisible()
    expect(fontFamilies(await editor.evaluate(element => getComputedStyle(element).fontFamily))).toEqual(fontFamilies(mono))
    await page.goto('/runs/run-prompt-review/reviews/int-prompt')
    const editable = page.getByRole('textbox', { name: 'System prompt', exact: true })
    await expect(editable).toBeVisible()
    await expect(editable).toHaveAttribute('contenteditable', 'true')
    expect(fontFamilies(await editable.evaluate(element => getComputedStyle(element).fontFamily))).toEqual(fontFamilies(mono))
  })
}

test('production bundle contains only the four approved brand font files', () => {
  const dist = fileURLToPath(new URL('../dist', import.meta.url))
  const fonts = readdirSync(dist, { recursive: true }).map(String).filter(path => /\.(woff2?|ttf|otf)$/i.test(path)).sort()
  expect(fonts).toEqual(['Bd', 'Blk', 'Md', 'Rg'].map(weight => `fonts/att/ATTAleckSans_W_${weight}.woff2`))
})
