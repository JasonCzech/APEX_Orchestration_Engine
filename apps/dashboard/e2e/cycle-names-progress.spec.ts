import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const timestamp = '2026-10-17T12:00:00Z'
const progress = { status: 'stale', approved: 1, total: 6, deliverables: [
  { kind: 'scope', version_id: 'scope', status: 'approved' },
  { kind: 'test_plan', version_id: 'plan', status: 'stale' },
  { kind: 'readiness', version_id: 'ready', status: 'in_review' },
  { kind: 'script', version_id: 'script', status: 'archived' },
  { kind: 'report', version_id: null, status: 'not_started' },
  { kind: 'postmortem', version_id: null, status: 'not_started' },
] }
for (const profile of ['viewer', 'app-admin', 'project-admin', 'global-admin'] as const) {
  test(`scoped names, copyable identities and bounded cycle progress for ${profile}`, async ({ page }) => {
    const editable = profile === 'project-admin' || profile === 'global-admin'
    const role = profile === 'viewer' ? 'viewer' : 'admin'
    const scopes = profile === 'global-admin' ? [] : [{ project_id: 'project-one', app_id: profile === 'app-admin' ? 'app-one' : null }, { project_id: 'project-two', app_id: profile === 'app-admin' ? 'app-two' : null }]
    let labels = [{ project_id: 'project-one', name: 'Payments', updated_at: timestamp }, { project_id: 'project-two', name: 'Payments', updated_at: timestamp }]
    let actorNames: Record<string, string> = { author: 'Morgan' }
    let namesDenied = false
    let detailReads = 0
    const writes: unknown[] = []
    const directoryReads: string[] = []
    const base = { project_id: 'project-one', app_id: 'app-one', environment_id: 'env-one', owner: 'author', owner_name: 'Morgan', mode: 'simulation', revision: 1, independent_review: true, created_at: timestamp, updated_at: timestamp }
    const summaries = [{ ...base, id: 'cycle-one', name: 'Checkout cycle', progress }, { ...base, id: 'cycle-two', name: 'Legacy checkout', project_id: 'project-two', app_id: 'app-two', owner: 'missing-author', owner_name: null, progress: null }]
    const versions = progress.deliverables.filter(item => item.version_id).map(item => ({ id: item.version_id, kind: item.kind, title: `${item.kind} evidence`, content: {}, dependencies: [], checksum: 'recorded-checksum', status: item.status === 'stale' ? 'approved' : item.status === 'archived' ? 'draft' : item.status, created_by: 'author', created_at: timestamp, archived: item.status === 'archived', reviews: [], approval_current: item.status === 'approved', validation_issues: [], available_actions: [] }))
    await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'names-browser-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/assistants/preset-one', route => route.fulfill({ json: { assistant_id: 'preset-one', graph_id: 'pipeline', name: 'Checkout preset', config: { configurable: { project_id: 'project-one', app_id: 'app-one' } }, context: {}, metadata: {}, created_at: timestamp, updated_at: timestamp, version: 1 } }))
    await page.route('**/v1/**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      let json: unknown = {}
      if (path.includes('/admin/consumers')) directoryReads.push(path)
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer: { id: 'reader', name: 'Current reader', role, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reader', name: 'Current reader', consumer_type: 'dashboard', role, scopes, is_unscoped: !scopes.length, mfa_required: false, step_up_required: false, capabilities: {} }
      else if (path === '/v1/catalog/projects') {
        if (namesDenied) return route.fulfill({ status: 403, json: { detail: 'Project names are unavailable' } })
        if (request.method() === 'PUT') { const body = request.postDataJSON(); writes.push(body); labels = labels.map(row => row.project_id === body.project_id ? { ...row, name: body.name } : row); json = labels.find(row => row.project_id === body.project_id) }
        else json = labels
      } else if (path === '/v1/catalog/applications') json = [{ id: 'app-one', project_id: 'project-one', name: 'Checkout API' }, { id: 'app-two', project_id: 'project-two', name: 'Other API' }]
      else if (path === '/v1/catalog/environments') json = [{ id: 'env-one', application_id: 'app-one', name: 'Staging', kind: 'test', base_url: null, hosts: [], options: {}, created_at: timestamp, updated_at: timestamp }]
      else if (path === '/v1/pipelines' || path === '/v1/engine-runs') json = { items: [], total: 0 }
      else if (path === '/v1/cycles') json = summaries
      else if (path === '/v1/cycles/cycle-one') { detailReads += 1; json = { ...summaries[0], owner_name: null, fields: {}, blockers: [], followups: [], versions } }
      else if (path === '/v1/cycles/cycle-one/actors') json = { names: actorNames, truncated: false }
      else if (path === '/v1/cycles/cycle-one/executions') json = { preparations: [], connections: [], linked_runs: [], recent_runs: [], revision: 1 }
      return route.fulfill({ json })
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/cycles')
    const first = page.getByRole('article').filter({ has: page.getByRole('link', { name: 'Checkout cycle', exact: true }) })
    const legacy = page.getByRole('article').filter({ has: page.getByRole('link', { name: 'Legacy checkout', exact: true }) })
    await expect(first.getByText('Payments', { exact: true })).toBeVisible()
    await expect(first.locator('code').filter({ hasText: /^project-one$/ })).toBeVisible()
    await expect(legacy.locator('code').filter({ hasText: /^project-two$/ })).toBeVisible()
    await expect(first.locator('p').filter({ hasText: /^Owner:/ })).toContainText('Morgan')
    await expect(legacy.locator('p').filter({ hasText: /^Owner:/ })).toContainText('Name unavailable')
    await expect(first.getByRole('region', { name: 'Deliverable progress' })).toContainText('Approval stale · 1/6 deliverables approved')
    await first.getByText('Deliverable states', { exact: true }).click()
    await expect(first.getByText('Script: Archived', { exact: true })).toBeVisible()
    await expect(first.getByText('Test plan: Approval stale', { exact: true })).toBeVisible()
    await expect(legacy.getByText('Deliverable progress unavailable')).toBeVisible()
    expect(detailReads).toBe(0)
    expect(directoryReads).toEqual([])
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (profile === 'project-admin') await page.screenshot({ path: fileURLToPath(new URL('../../../output/playwright/cycle-names-progress-mobile.png', import.meta.url)), fullPage: true })
    await first.getByRole('link', { name: 'Checkout cycle', exact: true }).click()
    await expect(page.getByRole('navigation', { name: 'Cycle workspace' })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Checkout cycle', exact: true })).toBeVisible()
    await expect(page.getByRole('region', { name: 'Deliverable progress' })).toContainText('1/6 deliverables approved')
    await expect(page.locator('p').filter({ hasText: /^Owner:/ })).toContainText('Morgan')
    const edit = page.getByRole('button', { name: 'Edit project name', exact: true })
    if (editable) {
      await edit.click()
      const dialog = page.getByRole('dialog', { name: 'Project display name', exact: true })
      await dialog.getByRole('textbox', { name: 'Project name', exact: true }).fill('Payments renamed')
      await dialog.getByRole('button', { name: 'Save', exact: true }).click()
      await expect(dialog).toHaveCount(0)
      await expect(page.getByText('Payments renamed', { exact: true })).toBeVisible()
      expect(writes).toEqual([{ project_id: 'project-one', name: 'Payments renamed' }])
    } else {
      await expect(edit).toHaveCount(0)
      expect(writes).toEqual([])
    }
    actorNames = { author: 'Morgan renamed' }
    await page.reload()
    await expect(page.locator('p').filter({ hasText: /^Owner:/ })).toContainText('Morgan renamed')
    actorNames = {}
    namesDenied = true
    await page.reload()
    await expect(page.locator('p').filter({ hasText: /^Owner:/ })).toContainText('Name unavailable')
    await expect(page.getByText('Project name unavailable', { exact: true })).toBeVisible()
    await expect(page.locator('code').filter({ hasText: /^author$/ })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Copy Actor ID', exact: true })).toBeVisible()
    expect(directoryReads).toEqual([])
    namesDenied = false
    await page.goto('/golden-configs/preset-one')
    const scope = page.getByRole('region', { name: 'Scope defaults' })
    await expect(scope.getByText(editable ? 'Payments renamed' : 'Payments', { exact: true })).toBeVisible()
    await expect(scope.getByRole('button', { name: 'Copy Project ID', exact: true })).toBeVisible()
  })
}
