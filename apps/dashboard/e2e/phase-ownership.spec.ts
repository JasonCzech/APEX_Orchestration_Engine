import { expect, test } from '@playwright/test'

test('phase ownership and analysis connectors remain explicit across engine changes and review', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'ownership-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/assistants/search', route => route.fulfill({ json: [] }))
  let draft: Record<string, unknown> | null = null
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = []
    if (path.startsWith('/v1/drafts') && ['POST', 'PUT'].includes(route.request().method())) {
      draft = { ...route.request().postDataJSON(), id: 'draft-ownership', created_at: '2026-10-22T00:00:00Z', updated_at: '2026-10-22T00:00:00Z' }; json = draft
    } else if (path === '/v1/drafts/draft-ownership') json = draft
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Reviewer', role: 'admin', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/system/info') json = { name: 'APEX', version: 'test', environment: 'test', features: {}, limits: {}, consumer: { id: 'reviewer', name: 'Reviewer', role: 'admin', scopes: [] } }
    else if (path === '/v1/pipelines/execution-readiness') json = { execution_selected: true, configuration_valid: true, requires_application: false }
    else if (path.startsWith('/v1/catalog/applications')) json = [{ id: 'app', project_id: 'ownership-project', name: 'Checkout', archived_at: null }]
    else if (path === '/v1/catalog/environments') json = [{ id: 'env', application_id: 'app', name: 'Staging', target_approved: true, base_url: 'https://staging.example.test' }]
    else if (path === '/v1/documents') json = { items: [], limit: 50, offset: 0 }
    return route.fulfill({ json })
  })
  await page.route('**/v1/pipelines/analysis/connections**', route => route.fulfill({ json: [
    { id: 'lra', name: 'LoadRunner Analysis', kind: 'analysis', provider: 'lra_engine' },
    { id: 'other-a', name: 'Metrics analysis', kind: 'analysis', provider: 'other' },
    { id: 'other-b', name: 'Trace analysis', kind: 'analysis', provider: 'other' },
  ] }))
  await page.goto('/runs/new')
  await page.getByLabel('Title', { exact: true }).fill('Ownership acceptance')
  await page.getByLabel('Request', { exact: true }).fill('Validate a mixed workflow')
  await page.getByRole('button', { name: 'Add backend', exact: true }).click()
  await page.getByRole('combobox', { name: /project$/i }).selectOption('ownership-project')
  await page.getByRole('combobox', { name: /application$/i }).selectOption('app')
  await page.getByRole('combobox', { name: /environment$/i }).selectOption('env')
  await page.getByRole('radio', { name: 'Primary system under test', exact: true }).check()
  await page.getByRole('tab', { name: 'Config', exact: true }).click()
  const owners = page.getByRole('group', { name: 'Phase execution', exact: true })
  await expect(owners.getByRole('button')).toHaveCount(7)
  const execution = owners.getByRole('button', { name: 'Execution', exact: true })
  await execution.focus()
  await execution.press('Space')
  await expect(execution).toHaveAttribute('aria-pressed', 'false')
  await page.getByRole('radio', { name: /^LoadRunner/ }).click()
  await page.getByRole('checkbox', { name: 'LRA Engine', exact: true }).check()
  await page.getByRole('combobox', { name: 'LRA Engine connector', exact: true }).selectOption('lra')
  await page.getByRole('checkbox', { name: 'Other Tools', exact: true }).check()
  await page.getByRole('listbox', { name: 'Other Tools connectors', exact: true }).selectOption(['other-a', 'other-b'])
  await page.getByRole('radio', { name: /^APEX Load/ }).click()
  await expect(page.getByRole('checkbox', { name: 'LRA Engine', exact: true })).toBeChecked()
  await expect(page.getByText('Incompatible with apex_load. Remove this selection.')).toBeVisible()
  await expect(page.getByRole('listbox', { name: 'Other Tools connectors', exact: true })).toHaveValues(['other-a', 'other-b'])
  await page.getByRole('checkbox', { name: 'LRA Engine', exact: true }).click()
  await expect(page.getByRole('checkbox', { name: 'LRA Engine', exact: true })).toHaveCount(0)
  await owners.getByRole('button', { name: 'Reporting & Analysis', exact: true }).click()
  await expect(page.getByText('The person completing Reporting must supply result evidence from each selected tool.')).toBeVisible()
  await page.getByRole('tab', { name: 'Review', exact: true }).click()
  await expect(page.getByRole('definition').filter({ hasText: 'Story Analysis — Agent' })).toContainText('Execution — Person')
  await expect(page.getByRole('definition').filter({ hasText: 'Story Analysis — Agent' })).toContainText('Reporting & Analysis — Person')
  await expect(page.getByRole('definition').filter({ hasText: 'Other Tools (Metrics analysis)' })).toBeVisible()
  await expect(page.getByRole('definition').filter({ hasText: 'Other Tools (Trace analysis)' })).toBeVisible()
  await page.getByRole('tab', { name: 'Config', exact: true }).click()
  await expect(owners.getByRole('button')).toHaveCount(7)
  await owners.screenshot({ path: '../../output/playwright/phase-ownership.png' })
})

test('human completion is distinct from review and submits the bound task output', async ({ page }) => {
  const phases = ['story_analysis', 'test_planning', 'env_triage', 'script_scenario', 'execution', 'reporting', 'postmortem']
  const payload = { schema_version: 1, kind: 'human_completion', phase: 'env_triage', task_id: 'env_triage-a1-human', revision: 1,
    actions: ['complete', 'abort'], sections: ['environment_readiness'], artifact_ids: [], analysis_selections: [],
    created_at: '2026-10-22T00:00:00Z', engine: 'sim', requires_load_test_spec: false, approved_target: null }
  let submitted: Record<string, unknown> | null = null
  const response = () => {
    const strip = phases.map((phase, index) => ({ phase, attempt: 1, executor: phase === 'env_triage' ? 'hitl' : 'langgraph', status: phase === 'env_triage' ? submitted ? 'succeeded' : 'awaiting_human_completion' : index < 2 ? 'succeeded' : 'pending', ...(phase === 'env_triage' && submitted ? { completed_by: 'reviewer' } : {}) }))
    const gate = { interrupt_id: 'human-1', kind: 'human_completion', phase: 'env_triage', payload }
    return { thread_id: 'mixed-human', title: 'Mixed ownership run', thread_status: submitted ? 'idle' : 'interrupted', current_phase: 'env_triage', project_id: 'project', phase_strip: strip,
      pending_gate: submitted ? null : gate, interrupts: submitted ? [] : [gate],
      values: { phases_plan: phases, current_phase: 'env_triage', phase_results: Object.fromEntries(strip.map(entry => [entry.phase, entry])) } }
  }
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'human-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = []
    if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Reviewer', consumer_type: 'dashboard', role: 'operator', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/system/info') json = { name: 'APEX', version: 'test', environment: 'test', features: {}, limits: {}, consumer: { id: 'reviewer', name: 'Reviewer', role: 'operator', scopes: [] } }
    else if (path === '/v1/pipelines/mixed-human/gates/human-1/resume') { submitted = route.request().postDataJSON(); return route.fulfill({ status: 202, json: { run_id: 'resumed' } }) }
    else if (path === '/v1/pipelines/mixed-human') json = response()
    else if (path.endsWith('/prompt-review')) return route.fulfill({ status: 404, json: { detail: 'No prompt review' } })
    return route.fulfill({ json })
  })
  await page.goto('/runs/mixed-human/phases/env_triage')
  await expect(page.getByRole('group', { name: 'Phase progress', exact: true }).getByRole('button')).toHaveCount(7)
  await page.getByRole('navigation', { name: 'Run sections' }).getByRole('link', { name: 'Review', exact: true }).click()
  await page.getByLabel('Summary', { exact: true }).fill('Environment verified by the operator')
  await page.getByLabel('environment readiness', { exact: true }).fill('Capacity and target verified')
  await page.getByRole('button', { name: 'Complete phase', exact: true }).click()
  await expect.poll(() => submitted).toMatchObject({ action: 'complete', task_id: 'env_triage-a1-human', expected_revision: 1, output: { deliverable: { summary: 'Environment verified by the operator', sections: { environment_readiness: 'Capacity and target verified' } } } })
  await page.goto('/runs/mixed-human/phases/env_triage')
  await expect(page.getByRole('group', { name: 'Phase progress', exact: true }).getByRole('button', { name: /Env Triage.*Person.*reviewer/ })).toBeVisible()
})
