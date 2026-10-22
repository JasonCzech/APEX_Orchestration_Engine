import { expect, test, type Page } from '@playwright/test'

async function fixture(page: Page, role = 'admin') {
  const rates = { input: '2.5', output: '10', cache_read: '1.25', cache_creation: '2.5' }
  const model = { id: 'model-1', model_key: 'gpt-4o', provider: 'openai', availability: 'available', version: 1, current_price: { ...rates, id: 'price-1', effective_from: '2026-01-01T00:00:00Z', effective_to: null } }
  const writes: { path: string; body: Record<string, unknown> }[] = []
  await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'pricing-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', async route => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    let json: unknown = {}
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'alice', name: 'Alice', role, scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'alice', name: 'Alice', consumer_type: 'dashboard', role, scopes: [], is_unscoped: role === 'admin', capabilities: {} }
    else if (request.method() === 'POST') {
      const body = request.postDataJSON() as Record<string, unknown>
      writes.push({ path, body })
      if (path.endsWith('/preview')) json = { preview_token: 'preview-1', events: 12, before_cost_usd: '1.00', after_cost_usd: '2.00', delta_cost_usd: '1.00', previously_unpriced: 0, calculated_at: '2026-10-22T12:00:00Z' }
      else if (path.endsWith('/corrections')) json = { id: 'job-1', state: 'queued', processed_count: 0 }
      else { model.version++; if (path.endsWith('/archive')) model.availability = 'archived'; if (path.endsWith('/restore')) model.availability = 'available'; json = model }
    } else if (path.endsWith('/corrections/job-1')) { model.version = Math.max(2, model.version); json = { id: 'job-1', state: 'succeeded', processed_count: 12 } }
    else if (path === '/v1/analytics/models') json = { items: [model], total: 1 }
    else if (path.endsWith('/prices')) json = { items: [{ id: 'revision-1', number: 1, reason: 'Initial rates', creator_name: 'Admin', created_at: '2026-01-01T00:00:00Z', published_at: '2026-01-01T00:00:00Z', intervals: [model.current_price] }], total: 1 }
    else if (path === '/v1/analytics/models/model-1') json = model
    else if (path.endsWith('/events')) json = { items: [{ id: 'event-1', at: '2026-10-20T12:00:00Z', model: 'gpt-4o', archived: model.availability === 'archived', project_id: 'p', app_id: 'App one', consumer_id: 'alice', consumer_name_snapshot: 'Alice', total_tokens: 1200, status: 'ok', original_cost_usd: '0.005', effective_cost_usd: '0.006' }], next_cursor: null, cost_visible: true }
    else if (path === '/v1/pipelines') json = { items: [], limit: 20, offset: 0 }
    return route.fulfill({ json })
  })
  return writes
}

test('administrator edits prices and retains immutable history', async ({ page }) => {
  const writes = await fixture(page)
  await page.goto('/analytics/models')
  await expect(page.getByRole('heading', { name: 'Model pricing', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Edit pricing' }).click()
  const panel = page.getByRole('dialog')
  await expect(panel).toBeVisible()
  await panel.getByLabel('Input', { exact: true }).fill('3.25')
  await panel.getByLabel('Reason', { exact: true }).fill('Updated contract')
  await panel.getByRole('button', { name: 'Save pricing' }).click()
  await expect(panel.getByText('Change saved.')).toBeVisible()
  expect(writes[0]?.body.rates).toMatchObject({ input: '3.25' })
  await panel.getByRole('button', { name: 'Pricing history', exact: true }).click()
  await expect(panel.getByText('Initial rates', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(panel).not.toBeVisible()
  await expect(page.getByRole('button', { name: 'Edit pricing' })).toBeFocused()
})

test('historical correction requires preview before confirmation', async ({ page }) => {
  const writes = await fixture(page)
  await page.goto('/analytics/models')
  await page.getByRole('button', { name: 'Edit pricing' }).click()
  const panel = page.getByRole('dialog')
  await panel.getByLabel('Effective timing').selectOption('correction')
  await panel.getByLabel('Correction start (local time)').fill('2026-10-01T00:00')
  await panel.getByLabel('Correction end, exclusive (local time)').fill('2026-10-02T00:00')
  await panel.getByLabel('Reason', { exact: true }).fill('Corrected contract period')
  await panel.getByRole('button', { name: 'Preview correction' }).click()
  await expect(panel.getByRole('heading', { name: 'Review historical correction' })).toBeVisible()
  expect(writes).toHaveLength(1)
  await panel.getByRole('button', { name: 'Confirm correction' }).click()
  await expect(panel.getByText(/Correction succeeded/)).toBeVisible()
  expect(writes[1]?.body.preview_token).toBe('preview-1')
  await panel.getByLabel('Effective timing').selectOption('now')
  await panel.getByLabel('Reason', { exact: true }).fill('Subsequent current rates')
  await panel.getByRole('button', { name: 'Save pricing' }).click()
  await expect.poll(() => writes.length).toBe(3)
  expect(writes[2]?.body.expected_version).toBe(2)
})

test('archive keeps usage and pricing accessible on desktop and mobile', async ({ page }) => {
  await fixture(page)
  await page.goto('/analytics/models')
  await page.screenshot({ path: '../../output/playwright/model-pricing-desktop.png', fullPage: true })
  await page.getByRole('button', { name: 'Edit pricing' }).click()
  const panel = page.getByRole('dialog')
  await panel.getByLabel('Reason', { exact: true }).fill('Retired model')
  await panel.getByRole('button', { name: 'Archive model', exact: true }).click()
  await panel.getByRole('button', { name: 'Confirm archive' }).click()
  await expect(panel.getByText('archived', { exact: true })).toBeVisible()
  await panel.getByRole('button', { name: 'Usage history', exact: true }).click()
  await expect(panel.getByRole('cell', { name: 'Alice' })).toBeVisible()
  await expect(panel.getByRole('cell', { name: 'gpt-4o Archived' })).toBeVisible()
  await page.setViewportSize({ width: 390, height: 844 })
  await page.screenshot({ path: '../../output/playwright/model-pricing-mobile.png', fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
})

test('regular users cannot open model administration', async ({ page }) => {
  await fixture(page, 'viewer')
  await page.goto('/analytics/models')
  await expect(page.getByText('Pricing management requires an unrestricted platform administrator.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Edit pricing' })).toHaveCount(0)
})

test('a stale write preserves input and offers explicit reload', async ({ page }) => {
  await fixture(page)
  await page.route('**/v1/analytics/models/model-1/prices', route => route.request().method() === 'POST' ? route.fulfill({ status: 409, json: { detail: 'Model changed; reload and review again' } }) : route.fallback())
  await page.goto('/analytics/models')
  await page.getByRole('button', { name: 'Edit pricing' }).click()
  const panel = page.getByRole('dialog')
  await panel.getByLabel('Input', { exact: true }).fill('4.5')
  await panel.getByLabel('Reason', { exact: true }).fill('New price')
  await panel.getByRole('button', { name: 'Save pricing' }).click()
  await expect(panel.getByRole('alert')).toContainText('Model changed')
  await expect(panel.getByLabel('Input', { exact: true })).toHaveValue('4.5')
  await panel.getByRole('button', { name: 'Reload model and reset draft' }).click()
  await expect(panel.getByLabel('Input', { exact: true })).toHaveValue('2.5')
})

test('rate shortcuts cannot reset a pending write and validation errors remain readable', async ({ page }) => {
  await fixture(page)
  let release: () => void = () => undefined
  const pending = new Promise<void>(resolve => { release = resolve })
  let writes = 0
  await page.route('**/v1/analytics/models/model-1/prices', async route => {
    if (route.request().method() !== 'POST') return route.fallback()
    writes += 1
    await pending
    return route.fulfill({ status: 422, json: { detail: [{ loc: ['body', 'rates', 'input'], msg: 'Rate precision exceeds nine decimal places' }] } })
  })
  await page.goto('/analytics/models')
  await page.getByRole('button', { name: 'Edit pricing' }).click()
  const panel = page.getByRole('dialog')
  await panel.getByLabel('Reason', { exact: true }).fill('Rate correction')
  await panel.getByRole('button', { name: 'Save pricing' }).click()
  await expect(panel.getByRole('button', { name: 'Saving…' })).toBeDisabled()
  for (const button of await panel.getByRole('button', { name: 'Use input rate' }).all()) await expect(button).toBeDisabled()
  await expect.poll(() => writes).toBe(1)
  release()
  await expect(panel.getByRole('alert')).toContainText('rates.input: Rate precision exceeds nine decimal places')
  await expect(panel.getByLabel('Reason', { exact: true })).toHaveValue('Rate correction')
})
