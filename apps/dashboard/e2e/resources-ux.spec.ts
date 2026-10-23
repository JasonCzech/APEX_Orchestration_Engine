import { expect, test } from '@playwright/test'

for (const role of ['viewer', 'operator', 'admin'] as const) {
  test(`document and evidence workflows support ${role} with keyboard and mobile layout`, async ({ page }) => {
    const doc = { id: 'document-full-identity', name: 'Checkout requirements.pdf', media_type: 'application/pdf', size_bytes: 2048, artifact_key: 'documents/spec', project_id: 'project-one', app_id: 'app-one', summary: 'Checkout source evidence', uploaded_by: 'author', created_at: '2026-10-17T12:00:00Z', text_preview: 'Bounded source text for the operator.' }
    const scoped = role !== 'admin' ? [{ project_id: 'project-one', app_id: null }] : []
    await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'resources-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'operator', name: 'Resource reviewer', role, scopes: scoped } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'operator', name: 'Resource reviewer', consumer_type: 'dashboard', role, scopes: scoped, is_unscoped: !scoped.length, capabilities: {} }
      else if (path === '/v1/catalog/applications') json = [{ id: 'app-one', project_ids: ['project-one'], name: 'Checkout API' }]
      else if (path === '/v1/documents') json = { items: [doc], total: 1 }
      else if (path === '/v1/documents/document-full-identity') json = doc
      else if (path === '/v1/pipelines') json = { items: ['run-a', 'run-b'].map((id, index) => ({ thread_id: id, title: 'Checkout release test', project_id: 'project-one', app_id: 'app-one', thread_status: 'idle', execution_result: 'passed', phase_strip: [{ phase: 'execution', status: 'succeeded' }], created_at: `2026-10-1${index + 6}T12:00:00Z`, updated_at: '2026-10-17T13:00:00Z' })), limit: 20, offset: 0 }
      else if (path === '/v1/context/evidence') json = [{ id: 'ev-one', title: 'Observed checkout', source: 'document', summary: 'Observed source result', thread_id: 'run-b', ref: null }]
      else if (path === '/v1/engine-runs') json = { items: [], total: 0 }
      return route.fulfill({ json })
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/context?tab=documents')
    await page.getByRole('button', { name: doc.name, exact: true }).click()
    const detail = page.getByRole('dialog', { name: `Document details: ${doc.name}` })
    await expect(detail.getByText(doc.text_preview)).toBeVisible()
    await expect(detail.getByRole('button', { name: 'Copy Document ID' })).toBeVisible()
    await expect(detail.getByRole('link', { name: 'Open test knowledge' })).toHaveAttribute('href', '/runs/new?step=context')
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (role === 'admin') await page.screenshot({ path: '../../output/playwright/resources-document-mobile.png', fullPage: true })
    await detail.getByRole('button', { name: 'Close' }).click()
    await page.getByRole('button', { name: `Document actions: ${doc.name}` }).click()
    await expect(page.getByRole('menuitem', { name: 'Delete…' })).toHaveCount(role === 'viewer' ? 0 : 1)
    await page.keyboard.press('Escape')
    await page.getByLabel('Filter by application').fill('app-other')
    await page.getByRole('button', { name: 'Apply', exact: true }).click()
    const filtered = page.getByRole('region', { name: 'No documents', exact: true })
    await expect(filtered).toHaveAttribute('data-state', 'filtered')
    await expect(filtered).toContainText('Nothing matches the current filters.')
    await filtered.getByRole('button', { name: 'Clear filters' }).click()
    await expect(page.getByRole('button', { name: doc.name, exact: true })).toBeVisible()
    await expect(page.getByLabel('Filter by application')).toHaveValue('')
    await page.goto('/evidence')
    const selector = page.getByRole('combobox', { name: 'Filter by run' })
    await selector.focus()
    await selector.fill('run-b')
    await expect(page.getByRole('listbox', { name: 'Filter by run choices' })).toContainText('Orchestration complete')
    await page.keyboard.press('Enter')
    await expect(selector).toHaveValue('Checkout release test')
    await expect(selector).toHaveAttribute('aria-describedby', /selected-description/)
    const filteredResponse = page.waitForResponse(response => response.url().includes('/v1/context/evidence?') && response.url().includes('thread_id=run-b'))
    await page.getByRole('button', { name: 'Apply filters', exact: true }).click()
    await filteredResponse
    await expect(page.getByRole('status', { name: /Loading evidence/ })).toHaveCount(0)
    await expect(page.getByRole('group', { name: 'Evidence controls' })).toContainText('1 active filter')
    await expect(page).toHaveURL(/thread=run-b/)
    await page.getByRole('tab', { name: 'Content', exact: true }).focus()
    await page.keyboard.press('ArrowRight')
    await expect(page.getByRole('tab', { name: 'Details', exact: true })).toBeFocused()
    await expect(page.getByRole('tabpanel')).toContainText('Evidence ID')
    if (role === 'admin') await page.screenshot({ path: '../../output/playwright/resources-evidence-mobile.png', fullPage: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  })
}
