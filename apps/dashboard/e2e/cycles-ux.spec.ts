import { expect, test } from '@playwright/test'

const version = { id: 'version-one-full-identity', kind: 'test_plan', title: 'Checkout plan', content: { objectives: 'Measure checkout', vusers: 10 }, checksum: 'abc123', dependencies: [], change_summary: 'Initial plan', status: 'in_review', created_by: 'author', created_at: '2026-10-17T12:00:00Z', archived: false, reviews: [], approval_current: false, validation_issues: [], available_actions: ['approve', 'request_revision', 'archive'] }
const original = { id: 'c1', name: 'Checkout cycle', project_id: 'project-one', app_id: 'app-one', environment_id: 'env-one', owner: 'reviewer', mode: 'simulation', revision: 3, independent_review: true, created_at: version.created_at, updated_at: version.created_at, blockers: [], followups: [], versions: [version], fields: { test_plan: [{ key: 'objectives', label: 'Objectives', control: 'multiline', required: true, help: '', integer: false, options: [] }, { key: 'vusers', label: 'VUsers', control: 'number', required: true, help: '', integer: true, minimum: 1, maximum: 1000, options: [] }] } }
const preparation = { id: 'prepared-execution-full-identity', created_at: version.created_at, created_by: 'reviewer', checked_at: version.created_at, launch_available: false, checks: [{ key: 'launch_authority', label: 'Launch ownership', status: 'blocked', detail: 'Preparation does not launch a pipeline.' }], snapshot: { plan_version_id: version.id, plan_title: version.title, plan_checksum: version.checksum, script_version_id: 'script-version', script_title: 'Checkout script', script_checksum: 'script-checksum', script_source_checksum: 'source-checksum', script_format: 'apex_native_json', readiness_version_id: null, readiness_checksum: null, engine: 'apex-load', mode: 'simulation', environment_id: 'env-one', environment_version: 1, connection_id: null, connection_version: null, launch_authority: 'pipeline', execution_policy: 'sequential', workload: { vusers: 10, ramp_s: 1, duration_s: 60 } } }

for (const profile of ['viewer', 'operator-unscoped', 'operator-scoped', 'admin'] as const) {
  test(`cycle review and editing honor ${profile} capability`, async ({ page }) => {
    const role = profile.startsWith('operator') ? 'operator' : profile
    const scopes = profile === 'operator-scoped' ? [{ project_id: 'project-one', app_id: null }] : []
    const canEdit = profile === 'operator-scoped' || profile === 'admin'
    let cycle = structuredClone(original)
    const actions: unknown[] = []
    const archives: unknown[] = []
    let archivedAt: string | null = null
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
      else if (path === `/v1/cycles/c1/executions/${preparation.id}/archive`) {
        archives.push(route.request().postDataJSON())
        archivedAt = '2026-10-17T13:00:00Z'
        cycle = { ...cycle, revision: cycle.revision + 1 }
        json = { id: preparation.id, revision: cycle.revision }
      }
      else if (path === `/v1/cycles/c1/executions/${preparation.id}/script-validations`) json = { revision: cycle.revision, blockers: archivedAt ? ['Preparation archived; prepare a new execution for further validation.'] : [], validations: [{ id: 'retained-validation', preparation_id: preparation.id, request_key: 'prior-validation', status: 'accepted', issues: [], warnings: [], created_by: 'original-validator', created_at: version.created_at, finished_at: version.created_at, current: !archivedAt }], validation_scope: 'Source only' }
      else if (path === '/v1/cycles/c1/executions') json = { revision: cycle.revision, preparations: [{ ...preparation, archived_at: archivedAt, archived_by: archivedAt ? 'reviewer' : null }], connections: [], linked_runs: [], recent_runs: [], inventory_snapshot_id: null, inventory_scanned_at: null, target_version: 1 }
      else if (path === '/v1/cycles') json = [cycle]
      else if (path === '/v1/engine-runs') json = { items: [], total: 0 }
      return route.fulfill({ json })
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/cycles/c1?tab=test_plan')
    await expect(page.getByRole('heading', { name: 'Checkout cycle', exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    async function verifyArchive() {
      const expectedRevision = cycle.revision
      await page.getByRole('button', { name: 'Executions', exact: true }).click()
      await expect(page.getByRole('heading', { name: 'Checkout script · apex-load', exact: true })).toBeVisible()
      if (!canEdit) {
        await expect(page.getByRole('button', { name: 'Archive preparation', exact: true })).toHaveCount(0)
        expect(archives).toHaveLength(0)
        return
      }
      await page.getByRole('button', { name: 'Archive preparation', exact: true }).click()
      const dialog = page.getByRole('dialog', { name: 'Archive execution preparation', exact: true })
      await expect(dialog).toContainText(preparation.id)
      await expect(dialog).toContainText(`cycle revision ${expectedRevision}`)
      expect(archives).toHaveLength(0)
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
      expect(archives).toHaveLength(0)
      await page.getByRole('button', { name: 'Archive preparation', exact: true }).click()
      await dialog.getByRole('button', { name: 'Archive preparation', exact: true }).click()
      await expect(page.getByText('Preparation archived: Checkout script.', { exact: true })).toBeVisible()
      expect(archives).toEqual([{ expected_revision: expectedRevision }])
      await expect(page.getByRole('heading', { name: 'Checkout script · apex-load', exact: true })).toHaveCount(0)
      await page.getByRole('checkbox', { name: 'Show archived preparations (1)', exact: true }).check()
      await expect(page.getByRole('heading', { name: 'Checkout script · apex-load', exact: true })).toBeVisible()
      await expect(page.getByText('Source accepted by provider', { exact: true })).toBeVisible()
      await expect(page.getByText(/original-validator/)).toBeVisible()
      await expect(page.getByRole('button', { name: 'Validate with APEX Load', exact: true })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Archive preparation', exact: true })).toHaveCount(0)
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    }
    if (!canEdit) {
      await expect(page.getByRole('button', { name: 'Revise as new draft' })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Approve this version', exact: true })).toHaveCount(0)
      await verifyArchive()
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
    await verifyArchive()
  })
}

for (const profile of ['operator-scoped', 'admin'] as const) {
  test(`execution preparation retries and new intents stay distinct for ${profile}`, async ({ page }) => {
    const role = profile === 'admin' ? 'admin' : 'operator'
    const scopes = profile === 'operator-scoped' ? [{ project_id: 'project-one', app_id: null }] : []
    const approved = { ...version, status: 'approved', approval_current: true, available_actions: [] }
    let cycle = { ...original, versions: [
      { ...approved, content: { vusers: 10, ramp_s: 1, duration_s: 60 } },
      { ...approved, id: 'script-version', title: 'Checkout script', kind: 'script', dependencies: [version.id], content: { engine: 'apex-load', format: 'apex_native_json' } },
    ] }
    let items: Array<typeof preparation & { archived_at: string | null; archived_by: string | null }> = []
    const receipts = new Map<string, string>()
    const requests: Array<{ idempotency_key: string; expected_revision: number }> = []
    await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'cycle-browser-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Cycle reviewer', role, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Cycle reviewer', consumer_type: 'dashboard', role, scopes, is_unscoped: !scopes.length, mfa_required: false, step_up_required: false, capabilities: {} }
      else if (path === '/v1/catalog/applications') json = [{ id: 'app-one', project_id: 'project-one', name: 'Checkout API' }]
      else if (path === '/v1/catalog/environments') json = [{ id: 'env-one', application_id: 'app-one', name: 'Performance staging' }]
      else if (path === '/v1/pipelines' || path === '/v1/engine-runs') json = { items: [], total: 0 }
      else if (path === '/v1/cycles/c1') json = cycle
      else if (path.endsWith('/archive')) {
        const id = path.split('/').at(-2)
        items = items.map(item => item.id === id ? { ...item, archived_at: '2026-10-17T13:00:00Z', archived_by: 'reviewer' } : item)
        cycle = { ...cycle, revision: cycle.revision + 1 }
        json = { id, revision: cycle.revision }
      }
      else if (path.endsWith('/script-validations')) json = { revision: cycle.revision, blockers: [], validations: [] }
      else if (path === '/v1/cycles/c1/executions' && route.request().method() === 'POST') {
        const body = route.request().postDataJSON()
        requests.push(body)
        let id = receipts.get(body.idempotency_key)
        if (!id) {
          id = `preparation-${receipts.size + 1}`
          receipts.set(body.idempotency_key, id)
          items = [...items, { ...preparation, id, archived_at: null, archived_by: null }]
          cycle = { ...cycle, revision: cycle.revision + 1 }
        }
        // The initial operation completed but the caller received no receipt.
        // Its retry must recover that operation before a later new intent.
        if (requests.length === 1) return route.fulfill({ status: 503, json: { detail: 'Preparation receipt unavailable' } })
        json = { id, revision: cycle.revision }
      }
      else if (path === '/v1/cycles/c1/executions') json = { revision: cycle.revision, preparations: items, connections: [], linked_runs: [], recent_runs: [], inventory_snapshot_id: null, inventory_scanned_at: null, target_version: 1 }
      return route.fulfill({ json })
    })
    await page.goto('/cycles/c1?tab=executions')
    await page.getByLabel('Approved test plan').selectOption(version.id)
    await page.getByLabel('Approved script').selectOption('script-version')
    await page.getByRole('button', { name: 'Prepare execution', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('Preparation receipt unavailable')
    await page.getByRole('button', { name: 'Prepare execution', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Archive preparation', exact: true })).toBeVisible()
    expect(requests).toHaveLength(2)
    expect(requests[1]!.idempotency_key).toBe(requests[0]!.idempotency_key)
    expect(items).toHaveLength(1)
    await page.getByRole('button', { name: 'Archive preparation', exact: true }).click()
    await page.getByRole('dialog').getByRole('button', { name: 'Archive preparation', exact: true }).click()
    await expect(page.getByText(/No active execution preparations/)).toBeVisible()
    await page.getByRole('button', { name: 'Prepare execution', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Archive preparation', exact: true })).toBeVisible()
    expect(requests).toHaveLength(3)
    expect(requests[2]!.idempotency_key).not.toBe(requests[1]!.idempotency_key)
    expect(requests[2]!.expected_revision).toBe(5)
    expect(items.filter(item => !item.archived_at)).toHaveLength(1)
    expect(items.filter(item => item.archived_at)).toHaveLength(1)
  })
}
