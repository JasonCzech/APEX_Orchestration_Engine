import { expect, test, type Page, type Route } from '@playwright/test'
import type { components } from '@apex/api-client'
import type { Cycle, Version } from '../src/features/cycles/api'

const at = '2026-10-17T12:00:00Z'
const field = (key: string, label: string, control = 'multiline', extra = {}) => ({ key, label, control, required: true, help: `${label} evidence is required before review.`, options: [], integer: false, ...extra })
const original: Version = { id: 'scope-1', kind: 'scope', title: 'Checkout scope', content: { requirements: 'Preserve checkout', exclusions: 'Mobile clients' }, checksum: 'scope-checksum', dependencies: [], change_summary: 'Initial scope', status: 'draft', created_by: 'reviewer', created_at: at, archived: false, reviews: [], approval_current: false, validation_issues: [], available_actions: ['archive', 'submit'] }
const initial: Cycle = {
  id: 'triage-cycle', name: 'Workflow triage', project_id: 'project-one', app_id: 'app-one', environment_id: 'env-one', owner: 'reviewer', mode: 'simulation', revision: 1, independent_review: false, created_at: at, updated_at: at, blockers: [], followups: [], versions: [original],
  fields: {
    scope: [field('requirements', 'Requirements'), field('exclusions', 'Exclusions')],
    test_plan: [field('vusers', 'Concurrent users', 'number', { integer: true, minimum: 1, maximum: 10000 }), field('arrival_rate', 'Arrival rate', 'number', { required: false })],
    readiness: [field('inventory_snapshot_id', 'Inventory snapshot ID', 'text')],
    script: [field('engine', 'Execution platform', 'select', { options: ['apex-load', 'loadrunner'] })],
    report: [field('executive_summary', 'Executive summary'), field('recommendation', 'Recommendation')],
  },
}

type Handler = (route: Route, path: string) => Promise<boolean>
async function install(page: Page, cycle: () => Cycle, handle?: Handler) {
  await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'triage-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (await handle?.(route, path)) return
    let json: unknown = {}
    const consumer = { id: 'reviewer', name: 'Scoped reviewer', role: 'operator', scopes: [{ project_id: 'project-one', app_id: null }] }
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: consumer.id, ...consumer, consumer_type: 'dashboard', is_unscoped: false, capabilities: {} }
    else if (path === '/v1/catalog/applications') json = [{ id: 'app-one', project_ids: ['project-one'], name: 'Checkout API' }]
    else if (path === '/v1/catalog/environments') json = [{ id: 'env-one', application_id: 'app-one', name: 'Performance staging' }]
    else if (path === '/v1/cycles/triage-cycle') json = cycle()
    else if (path === '/v1/cycles') json = [cycle()]
    else if (path.endsWith('/provider-scripts')) json = { connections: [], scripts: [], imports: [], total: 0, revision: cycle().revision, blockers: [] } satisfies components['schemas']['ProviderScriptsRead']
    else if (path.endsWith('/executions')) json = { revision: cycle().revision, preparations: [], connections: [], linked_runs: [], recent_runs: [], inventory_snapshot_id: null, inventory_scanned_at: null, target_version: 1 }
    else if (path === '/v1/inventory/environments/env-one') json = { environment_id: 'env-one', stale: false, snapshot: { id: 'inventory-exact-id', scanned_at: at, services: [] } }
    else if (path === '/v1/pipelines' || path === '/v1/engine-runs') json = { items: [], limit: 20, offset: 0 }
    else if (path.endsWith('/results')) json = { results: [], read_at: at, baseline_id: null }
    await route.fulfill({ json })
  })
}

test('every cycle review action confirms exact identity and clears its note, with visible revision history', async ({ page }) => {
  let cycle = structuredClone(initial)
  const actions: Array<{ action: string; version_id: string; expected_revision: number; note: string }> = []
  await install(page, () => cycle, async (route, path) => {
    if (!path.endsWith('/actions')) return false
    const body = route.request().postDataJSON()
    actions.push(body)
    expect(body.version_id).toBe(original.id)
    expect(body.expected_revision).toBe(cycle.revision)
    const status = body.action === 'submit' ? 'in_review' : body.action === 'approve' ? 'approved' : body.action === 'request_revision' ? 'draft' : cycle.versions[0]!.status
    const available = body.action === 'archive' ? ['restore'] : status === 'draft' ? ['archive', 'submit'] : status === 'in_review' ? ['archive', 'approve', 'request_revision'] : ['archive']
    cycle = { ...cycle, revision: cycle.revision + 1, versions: [{ ...cycle.versions[0]!, status, archived: body.action === 'archive', approval_current: status === 'approved' && body.action !== 'archive', available_actions: available, reviews: [...cycle.versions[0]!.reviews, { action: body.action, actor: 'reviewer', at, note: body.note }] }] }
    await route.fulfill({ json: cycle })
    return true
  })
  await page.goto('/cycles/triage-cycle?tab=scope')
  const version = page.getByRole('region', { name: 'Selected deliverable version' })
  await expect(version).toContainText('Created by Scoped reviewer')
  const labels = [
    ['Submit for review', 'Submitted for review'], ['Request revision', 'Revision requested'],
    ['Submit for review', 'Submitted for review'], ['Approve this version', 'Version approved'],
    ['Archive in APEX', 'Version archived'], ['Restore in APEX', 'Version restored'],
  ]
  for (const [label, result] of labels) {
    const before = actions.length
    await version.getByLabel('Review note', { exact: true }).fill(`Note for ${label}`)
    if (label === 'Submit for review') {
      const buttons = await version.locator('.cycle-actions > button').allTextContents()
      expect(buttons.at(-1)).toBe('Archive in APEX')
      await expect(version.getByRole('button', { name: 'Archive in APEX', exact: true })).toHaveClass(/btn-danger/)
    }
    await version.getByRole('button', { name: label, exact: true }).click()
    const dialog = page.getByRole('dialog', { name: label, exact: true })
    await expect(dialog).toContainText(original.id)
    await expect(dialog).toContainText(`cycle revision ${cycle.revision}`)
    expect(actions).toHaveLength(before)
    await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()
    expect(actions).toHaveLength(before)
    await version.getByRole('button', { name: label, exact: true }).click()
    await dialog.getByRole('button', { name: `Confirm ${label.toLowerCase()}`, exact: true }).click()
    await expect(version.getByRole('status').filter({ hasText: `${result}:` })).toHaveText(`${result}: ${original.title}.`)
    await expect(version.getByLabel('Review note', { exact: true })).toHaveValue('')
    if (label === 'Request revision') await expect(version.locator('.tonal-card.warning')).toContainText('Note for Request revision')
  }
  await version.getByText('Version identity and review history', { exact: true }).click()
  await expect(version.locator('details')).toContainText('Revision requested · Scoped reviewer')
  expect(actions.map(action => action.action)).toEqual(['submit', 'request_revision', 'submit', 'approve', 'archive', 'restore'])
})

test('cycle conflict recovery preserves text and explicitly selects the newer replacement base', async ({ page }) => {
  let cycle = structuredClone(initial)
  const writes: Array<Record<string, unknown>> = []
  await install(page, () => cycle, async (route, path) => {
    if (!path.endsWith('/versions')) return false
    const body = route.request().postDataJSON()
    writes.push(body)
    if (writes.length === 1) {
      cycle = { ...cycle, revision: 2, versions: [...cycle.versions, { ...original, id: 'scope-2', replaces: original.id, title: 'Parallel author scope' }] }
      await route.fulfill({ status: 409, json: { detail: 'Another author changed the cycle.' } })
    } else {
      expect(body.expected_revision).toBe(2)
      expect(body.replaces).toBe('scope-2')
      cycle = { ...cycle, revision: 3, versions: [...cycle.versions, { ...original, ...body, id: 'scope-3', content: body.content }] }
      await route.fulfill({ json: cycle })
    }
    return true
  })
  await page.goto('/cycles/triage-cycle?tab=scope')
  await page.getByRole('button', { name: 'Revise as new draft' }).click()
  await page.getByLabel('Requirements *').fill('Authored requirements must survive')
  await page.getByLabel('Change summary and affected requirements').fill('Preserve local edits')
  await page.getByRole('button', { name: 'Save draft version' }).click()
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Your text is retained')
  await expect(alert).toHaveCSS('padding', /[1-9]/)
  await page.getByRole('button', { name: 'Keep my text and load the latest revision' }).click()
  await expect(alert).toContainText('Parallel author scope')
  await expect(page.getByLabel('Requirements *')).toHaveValue('Authored requirements must survive')
  await page.getByRole('button', { name: 'Keep my text and revise the latest version' }).click()
  await page.getByRole('button', { name: 'Save draft version' }).click()
  await expect(page).toHaveURL(/version=scope-3/)
  expect(writes).toHaveLength(2)
  await expect(page.getByRole('region', { name: 'Selected deliverable version' })).toContainText('Authored requirements must survive')
})

test('cycle authoring exposes real field contracts, inventory identity and unconfigured provider guidance', async ({ page }) => {
  const cycle = structuredClone(initial)
  await install(page, () => cycle)
  await page.goto('/cycles/triage-cycle?tab=overview')
  await expect(page.getByText('Next action: Complete and submit the scope for review', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Executions', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Test plan', exact: true }).click()
  await page.getByRole('button', { name: 'Create draft', exact: true }).click()
  const vusers = page.getByLabel('Concurrent users *')
  await expect(vusers).toHaveAttribute('aria-required', 'true')
  await expect(vusers).toHaveAttribute('step', '1')
  await vusers.fill('1200.75')
  expect(await vusers.evaluate(input => (input as HTMLInputElement).validity.stepMismatch)).toBe(true)
  await expect(page.getByText(/Arrival-rate scheduling is not supported/)).toBeVisible()
  await page.getByRole('button', { name: 'Readiness', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Discard and leave' }).click()
  await page.getByRole('button', { name: 'Create draft', exact: true }).click()
  const inventory = page.getByLabel('Inventory snapshot ID *')
  await expect(inventory).toHaveAttribute('type', 'text')
  await expect(inventory).toHaveAttribute('aria-describedby', /help/)
  await expect(page.getByRole('link', { name: 'Review environment inventory' })).toHaveAttribute('href', '/environments/env-one')
  await page.getByRole('button', { name: 'Use latest inventory snapshot' }).click()
  await expect(inventory).toHaveValue('inventory-exact-id')
  await page.getByRole('button', { name: 'Scripts', exact: true }).click()
  await page.getByRole('dialog').getByRole('button', { name: 'Discard and leave' }).click()
  await page.getByText('Import from the APEX Load library', { exact: true }).click()
  await expect(page.getByRole('region', { name: 'No APEX Load connection available' })).toContainText('Ask an administrator')
  await page.getByRole('button', { name: 'Create draft', exact: true }).click()
  await expect(page.getByLabel('Execution platform *').getByRole('option', { name: 'APEX Load', exact: true })).toHaveAttribute('value', 'apex-load')
})

for (const verified of [true, false, undefined]) {
test(`report capture preserves binding verification (${String(verified)}) and selects the exact new version`, async ({ page }) => {
  const report = { ...original, id: 'report-2', kind: 'report', title: 'Release report', content: { executive_summary: 'Current summary', recommendation: 'Review the evidence' } }
  let cycle: Cycle = { ...structuredClone(initial), versions: [{ ...report, id: 'report-1', content: { ...report.content, executive_summary: 'Earlier summary' } }, report] }
  const result = { id: 'engine-run-1', thread_id: 'run-1', engine: 'apex-load', attempt: 1, execution_status: 'completed', test_outcome: null, evidence_status: 'recorded', started_at: at, ended_at: at, metrics: [{ key: 'tps', label: 'Throughput', unit: 'tps', value: 0, preference: 'higher', difference: null, percent_difference: null }], sla_breaches: [], warnings: [], evidence_checksum: 'a'.repeat(64), ...(verified === undefined ? {} : { bindings_verified: verified }) }
  await install(page, () => cycle, async (route, path) => {
    if (path.endsWith('/results')) { await route.fulfill({ json: { results: [result], read_at: at, baseline_id: null } }); return true }
    if (path.endsWith('/reports/report-2/evidence')) {
      expect(route.request().postDataJSON().expected_results).toEqual({ 'engine-run-1': 'a'.repeat(64) })
      cycle = { ...cycle, revision: 2, versions: [...cycle.versions, { ...report, id: 'captured-report-3', replaces: 'report-2', title: 'Release report (evidence captured 2026-10-17T12:00:00Z)', content: { ...report.content, _execution_evidence: { captured_at: at, captured_by: 'reviewer', results: [result] } } }] }
      await route.fulfill({ json: cycle }); return true
    }
    return false
  })
  await page.goto('/cycles/triage-cycle?tab=report&version=report-2')
  const version = page.getByRole('region', { name: 'Selected deliverable version' })
  await version.getByLabel('Compare with version').selectOption('report-1')
  await expect(version.getByRole('heading', { name: 'Executive summary · Changed' })).toBeVisible()
  await expect(version.getByRole('heading', { name: 'Recommendation', exact: true })).toHaveCount(0)
  await version.getByRole('checkbox', { name: 'Show unchanged (1)' }).check()
  await expect(version.getByRole('heading', { name: 'Recommendation', exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Capture evidence as new report draft' }).click()
  await expect(page).toHaveURL(/version=captured-report-3/)
  await expect(page.getByLabel('Selected version')).toHaveValue('captured-report-3')
  await expect(page.getByRole('region', { name: 'Frozen report evidence' })).toContainText('test outcome Not available')
  await expect(page.getByRole('region', { name: 'Frozen report evidence' }).getByRole('definition')).toHaveText('0')
  const evidence = page.getByRole('region', { name: 'Frozen report evidence' })
  await expect(evidence).toContainText(verified ? 'Approved bindings verified.' : 'Bindings remain unverified.')
  await expect(evidence).not.toContainText(verified ? 'Bindings remain unverified.' : 'Approved bindings verified.')
  await expect(evidence).toContainText('This captures history summaries, not raw provider artifacts.')
})
}

test('prompt version activation is explicit and accepted resource jobs distinguish execution and run identities', async ({ page }) => {
  let active = { id: 'version-1', version: 1 }
  let content = 'Review the source evidence.'
  const saved: unknown[] = []
  const prompt = () => ({ id: 'prompt-one', namespace: 'phase', key: 'story_analysis/system', description: 'Story review', active_version: active, archived_at: null, updated_at: at, content, note: null })
  await install(page, () => initial, async (route, path) => {
    let json: unknown
    let status = 200
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Global administrator', role: 'admin', scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', id: 'reviewer', name: 'Global administrator', role: 'admin', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/prompts') json = [prompt()]
    else if (path === '/v1/prompts/prompt-one') json = prompt()
    else if (path === '/v1/prompts/prompt-one/versions') {
      if (route.request().method() === 'POST') {
        const body = route.request().postDataJSON()
        saved.push(body)
        content = body.content
        active = { id: 'version-2', version: 2 }
        json = { ...active, content, note: body.note, created_by: 'reviewer', created_at: at, parent_version_id: 'version-1' }
        status = 201
      } else json = [{ ...active, note: null, created_by: 'reviewer', created_at: at, parent_version_id: null }]
    } else if (path.startsWith('/v1/prompts/prompt-one/versions/')) json = { ...active, content, note: null, created_by: 'reviewer', created_at: at, parent_version_id: null }
    else if (path === '/v1/prompts/prompt-one/test') {
      expect(route.request().postDataJSON().version_id).toBe('version-2')
      json = { run_id: 'native-template-execution', thread_id: 'template-run' }; status = 202
    } else if (path === '/v1/context/summaries') {
      expect(route.request().postDataJSON().subject).toBe('Review release context')
      json = { run_id: 'native-summary-execution', stream_url: '/threads/summary-run/runs/native-summary-execution/stream' }; status = 202
    } else if (path === '/v1/documents') json = { items: [], total: 0 }
    else return false
    await route.fulfill({ json, status })
    return true
  })
  await page.goto('/prompts/phase/story_analysis%2Fsystem')
  await expect(page.getByRole('heading', { level: 2, name: 'story_analysis/system', exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Archive', exact: true })).toHaveCount(0)
  const actions = page.getByRole('button', { name: 'Prompt actions: story_analysis/system', exact: true })
  await actions.focus()
  await page.keyboard.press('ArrowDown')
  await expect(page.getByRole('menuitem', { name: 'Archive…', exact: true })).toBeFocused()
  await page.keyboard.press('Enter')
  await page.getByRole('dialog', { name: 'Archive prompt?', exact: true }).getByRole('button', { name: 'Cancel', exact: true }).click()
  await expect(actions).toBeFocused()
  await page.getByRole('button', { name: 'New version', exact: true }).click()
  await expect(page.getByText('Saving makes this the active catalog version immediately.', { exact: false })).toBeVisible()
  const save = page.getByRole('button', { name: 'Save and activate v2', exact: true })
  await expect(save).toBeDisabled()
  await page.getByRole('textbox', { name: 'New version content', exact: true }).fill('Review complete evidence and cite the source.')
  await save.click()
  await expect(page.getByText('active v2', { exact: true })).toBeVisible()
  expect(saved).toEqual([{ content: 'Review complete evidence and cite the source.' }])
  await page.goto('/prompts/phase/story_analysis%2Fsystem/playground')
  await page.getByRole('button', { name: 'Check template', exact: true }).click()
  const template = page.getByTestId('playground-accepted')
  await expect(template).toContainText('Execution ID: native-template-execution')
  await expect(template).toContainText('Run ID: template-run')
  await expect(template.getByRole('link', { name: 'Open run', exact: true })).toHaveAttribute('href', '/runs/template-run')
  await expect(page.getByTestId('playground-history')).toContainText('Run ID: template-run')
  await page.goto('/context?tab=summaries')
  await page.getByRole('textbox', { name: 'Summary subject', exact: true }).fill('Review release context')
  await page.getByRole('combobox', { name: 'Project id', exact: true }).fill('project-one')
  await page.getByRole('button', { name: 'Generate summary', exact: true }).click()
  const summary = page.getByTestId('summary-accepted')
  await expect(summary).toContainText('Execution ID: native-summary-execution')
  await expect(summary).toContainText('Run ID: summary-run')
  await expect(summary.getByRole('link', { name: 'Open run', exact: true })).toHaveAttribute('href', '/runs/summary-run')
  await expect(page.getByTestId('summary-history')).toContainText('Run ID: summary-run')
})
