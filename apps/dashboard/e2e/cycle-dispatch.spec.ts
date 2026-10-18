import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

for (const [profile, scenario] of [['viewer', 'recover'], ['operator-unscoped', 'recover'], ['operator-scoped', 'recover'], ['admin', 'recover'], ['operator-scoped', 'cancel'], ['admin', 'cancel']] as const) {
  test(`dispatch confirmation and durable ${scenario} for ${profile}`, async ({ page }) => {
    const canEdit = profile === 'operator-scoped' || profile === 'admin'
    const role = profile.startsWith('operator') ? 'operator' : profile
    const scopes = profile === 'operator-scoped' ? [{ project_id: 'project', app_id: null }] : []
    const timestamp = '2026-10-17T12:00:00Z'
    const cycle = { id: 'cycle', name: 'Approved checkout cycle', project_id: 'project', app_id: 'application', environment_id: 'environment', owner: 'operator', mode: 'live', revision: 7, independent_review: true, created_at: timestamp, updated_at: timestamp, blockers: [], followups: [], versions: [], fields: {} }
    const preparation = { id: 'preparation', created_at: timestamp, created_by: 'operator', checked_at: timestamp, archived_at: null, archived_by: null, checks: [], launch_available: false, snapshot: { plan_version_id: 'plan', plan_title: 'Approved plan', plan_checksum: 'plan-checksum', script_version_id: 'script', script_title: 'Approved checkout', script_checksum: 'script-checksum', script_source_checksum: 'source-checksum', script_format: 'apex_native_json', readiness_version_id: 'readiness', readiness_checksum: 'readiness-checksum', engine: 'apex-load', mode: 'live', environment_id: 'environment', environment_version: 2, connection_id: 'connection', connection_version: timestamp, launch_authority: 'pipeline', execution_policy: 'sequential', workload: { vusers: 1, ramp_s: 1, duration_s: 5 } } }
    type Attempt = { id: string; preparation_id: string; request_key: string; status: string; created_by: string; created_at: string; updated_at: string; thread_id: string | null; run_id: string | null; engine_run_id: string | null; external_run_id: string | null; detail: string }
    let attempt: Attempt | null = null
    let revision = 7
    const requests: Array<{ expected_revision: number; idempotency_key: string }> = []
    const cancellations: Array<{ dispatch_id: string }> = []
    await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'dispatch-browser-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer: { id: 'operator', name: 'Cycle operator', role, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'operator', name: 'Cycle operator', consumer_type: 'dashboard', role, scopes, is_unscoped: !scopes.length, mfa_required: false, step_up_required: false, capabilities: {} }
      else if (path === '/v1/catalog/applications') json = [{ id: 'application', project_id: 'project', name: 'Checkout API' }]
      else if (path === '/v1/catalog/environments') json = [{ id: 'environment', application_id: 'application', name: 'Performance staging' }]
      else if (path === '/v1/catalog/projects') json = [{ project_id: 'project', name: 'Checkout project', updated_at: timestamp }]
      else if (path === '/v1/pipelines' || path === '/v1/engine-runs') json = { items: [], total: 0 }
      else if (path === '/v1/cycles/cycle') json = { ...cycle, revision }
      else if (path === '/v1/cycles/cycle/actors') json = { names: { operator: 'Cycle operator' }, truncated: false }
      else if (path === '/v1/cycles') json = [{ ...cycle, revision }]
      else if (path.endsWith('/script-validations')) json = { revision, blockers: [], validations: [] }
      else if (path === '/v1/cycles/cycle/executions') json = { revision, preparations: [preparation], connections: [{ id: 'connection', name: 'Local Load', provider: 'apex_load', runtime_version: timestamp }], linked_runs: [], recent_runs: [], inventory_snapshot_id: 'scan', inventory_scanned_at: timestamp, target_version: 2 }
      else if (path.endsWith('/dispatch/cancel')) {
        const body = route.request().postDataJSON()
        cancellations.push(body)
        expect(body.dispatch_id).toBe(attempt?.id)
        if (attempt) attempt = { ...attempt, status: 'aborted', detail: 'Unsubmitted launch intent canceled; no execution was submitted.' }
        json = { revision, checks: [], can_dispatch: false, can_resume: false, can_cancel: false, attempt }
      }
      else if (path.endsWith('/dispatch')) {
        if (route.request().method() === 'POST') {
          const body = route.request().postDataJSON()
          requests.push(body)
          if (!attempt) {
            revision = 8
            attempt = { id: 'dispatch-receipt', preparation_id: 'preparation', request_key: body.idempotency_key, status: 'uncertain', created_by: 'operator', created_at: timestamp, updated_at: timestamp, thread_id: null, run_id: null, engine_run_id: null, external_run_id: null, detail: 'The launch receipt is unconfirmed. Recover the original operation.' }
          } else {
            expect(body.idempotency_key).toBe(attempt.request_key)
            attempt = { ...attempt, status: 'submitted', thread_id: 'original-thread', run_id: 'original-run', detail: 'Original execution submitted.' }
          }
        }
        json = { revision, checks: canEdit ? [] : [{ key: 'permission', label: 'Launch permission', status: 'blocked', detail: 'Operator access to this cycle is required.' }], can_dispatch: canEdit && !attempt, can_resume: canEdit && attempt?.status === 'uncertain', can_cancel: canEdit && attempt?.status === 'uncertain' && !attempt.thread_id, attempt }
      }
      return route.fulfill({ json })
    })
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto('/cycles/cycle?tab=executions')
    await expect(page.getByRole('heading', { name: 'Approved checkout cycle', exact: true })).toBeVisible()
    await expect(page.getByText(canEdit ? 'Ready for launch review' : 'Launch blocked', { exact: true })).toBeVisible()
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (!canEdit) {
      await expect(page.getByRole('button', { name: 'Launch approved execution', exact: true })).toHaveCount(0)
      expect(requests).toHaveLength(0)
      return
    }
    const launch = page.getByRole('button', { name: 'Launch approved execution', exact: true })
    await launch.click()
    let dialog = page.getByRole('dialog', { name: 'Launch approved execution', exact: true })
    await expect(dialog).toContainText('Performance staging')
    await expect(dialog).toContainText('1 VUsers · 1s ramp · 5s duration')
    expect(requests).toHaveLength(0)
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(launch).toBeFocused()
    await launch.click()
    dialog = page.getByRole('dialog', { name: 'Launch approved execution', exact: true })
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
    if (profile === 'operator-scoped' && scenario === 'recover') await page.screenshot({ path: fileURLToPath(new URL('../../../output/playwright/cycle-dispatch-mobile.png', import.meta.url)) })
    await dialog.getByRole('button', { name: 'Confirm launch', exact: true }).click()
    await expect(page.getByText('Dispatch outcome unconfirmed', { exact: true })).toBeVisible()
    expect(requests).toHaveLength(1)
    await page.reload()
    await expect(page.getByRole('button', { name: 'Resume dispatch', exact: true })).toBeVisible()
    expect(requests).toHaveLength(1)
    if (scenario === 'cancel') {
      await page.getByRole('button', { name: 'Cancel unsubmitted launch', exact: true }).click()
      const cancelDialog = page.getByRole('dialog', { name: 'Cancel unsubmitted launch', exact: true })
      expect(cancellations).toHaveLength(0)
      await cancelDialog.getByRole('button', { name: 'Keep launch intent', exact: true }).click()
      expect(cancellations).toHaveLength(0)
      await page.getByRole('button', { name: 'Cancel unsubmitted launch', exact: true }).click()
      await cancelDialog.getByRole('button', { name: 'Confirm cancel launch', exact: true }).click()
      await expect(page.getByText('Dispatch canceled', { exact: true })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Resume dispatch', exact: true })).toHaveCount(0)
      expect(cancellations).toEqual([{ dispatch_id: 'dispatch-receipt' }])
      expect(requests).toHaveLength(1)
      return
    }
    await page.getByRole('button', { name: 'Resume dispatch', exact: true }).click()
    await page.getByRole('dialog', { name: 'Resume execution dispatch', exact: true }).getByRole('button', { name: 'Confirm resume dispatch', exact: true }).click()
    await expect(page.getByRole('link', { name: 'Open execution and results', exact: true })).toHaveAttribute('href', '/runs/original-thread/phases/execution')
    expect(requests).toHaveLength(2)
    expect(requests[1]?.idempotency_key).toBe(requests[0]?.idempotency_key)
    expect(requests[1]?.expected_revision).toBe(8)
  })
}
