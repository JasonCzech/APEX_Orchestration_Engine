import { expect, test } from '@playwright/test'

const version = { id: 'version-one-full-identity', kind: 'test_plan', title: 'Checkout plan', content: { objectives: 'Measure checkout', vusers: 10 }, checksum: 'abc123', dependencies: [], change_summary: 'Initial plan', status: 'in_review', created_by: 'author', created_at: '2026-10-17T12:00:00Z', archived: false, reviews: [], approval_current: false, validation_issues: [], available_actions: ['approve', 'request_revision', 'archive'] }
const original = { id: 'c1', name: 'Checkout cycle', project_id: 'project-one', app_id: 'app-one', environment_id: 'env-one', owner: 'reviewer', mode: 'simulation', revision: 3, independent_review: true, created_at: version.created_at, updated_at: version.created_at, blockers: [], followups: [], versions: [version], fields: { test_plan: [{ key: 'objectives', label: 'Objectives', control: 'multiline', required: true, help: '', integer: false, options: [] }, { key: 'vusers', label: 'VUsers', control: 'number', required: true, help: '', integer: true, minimum: 1, maximum: 1000, options: [] }] } }

for (const profile of ['viewer', 'operator-unscoped', 'operator-scoped', 'admin'] as const) {
  test(`cycle review and editing honor ${profile} capability`, async ({ page }) => {
    const role = profile.startsWith('operator') ? 'operator' : profile
    const scopes = profile === 'operator-scoped' ? [{ project_id: 'project-one', app_id: null }] : []
    const canEdit = profile === 'operator-scoped' || profile === 'admin'
    let cycle = structuredClone(original)
    const actions: unknown[] = []
    await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'cycle-browser-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Cycle reviewer', role, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Cycle reviewer', consumer_type: 'dashboard', role, scopes, is_unscoped: !scopes.length, mfa_required: false, step_up_required: false, capabilities: {} }
      else if (path === '/v1/catalog/applications') json = [{ id: 'app-one', project_id: 'project-one', name: 'Checkout API' }]
      else if (path === '/v1/catalog/environments') json = [{ id: 'env-one', application_id: 'app-one', name: 'Performance staging' }]
      else if (path === '/v1/pipelines') json = { items: [], limit: 20, offset: 0 }
      else if (path === '/v1/cycles/c1') json = { ...cycle, versions: cycle.versions.map(item => ({ ...item, available_actions: canEdit ? item.available_actions : [] })) }
      else if (path === '/v1/cycles/c1/actions') {
        const action = route.request().postDataJSON()
        actions.push(action)
        cycle = { ...cycle, revision: 4, versions: [{ ...version, status: 'approved', approval_current: true, available_actions: ['archive'] }] }
        json = cycle
      }
      else if (path === '/v1/cycles/c1/executions') json = { revision: 3, preparations: [], connections: [], linked_runs: [], recent_runs: [] }
      else if (path === '/v1/cycles') json = [cycle]
      else if (path === '/v1/engine-runs') json = { items: [], total: 0 }
      return route.fulfill({ json })
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/cycles/c1?tab=test_plan')
    await expect(page.getByRole('heading', { name: 'Checkout cycle', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (!canEdit) {
      await expect(page.getByRole('button', { name: 'Revise as new draft' })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Approve this version', exact: true })).toHaveCount(0)
      return
    }
    await page.getByRole('button', { name: 'Revise as new draft' }).click()
    await page.getByLabel('Objectives *').fill('Retain this authored objective')
    await page.getByRole('button', { name: 'Scope', exact: true }).click()
    const guard = page.getByRole('dialog', { name: 'Unsaved deliverable changes' })
    await expect(guard).toBeVisible()
    await guard.getByRole('button', { name: 'Keep editing' }).click()
    await expect(page.getByLabel('Objectives *')).toHaveValue('Retain this authored objective')
    await page.getByRole('button', { name: 'Cancel editing' }).click()
    await guard.getByRole('button', { name: 'Discard and leave' }).click()
    await page.getByRole('button', { name: 'Approve this version', exact: true }).click()
    const confirm = page.getByRole('dialog', { name: 'Approve this version', exact: true })
    await expect(confirm).toContainText('version-one-full-identity')
    expect(actions).toHaveLength(0)
    await confirm.getByRole('button', { name: 'Confirm approve this version' }).click()
    await expect(page.getByText('Version approved: Checkout plan.')).toBeVisible()
    expect(actions).toEqual([{ action: 'approve', version_id: version.id, expected_revision: 3, note: '' }])
  })
}
