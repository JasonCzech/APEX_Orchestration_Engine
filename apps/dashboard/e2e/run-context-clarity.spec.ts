import { expect, test } from '@playwright/test'

test('demo activity offers saved reasoning and resolves the application consistently', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  await page.goto('/runs/run-active')
  const overview = page.getByRole('region', { name: 'Run overview', exact: true })
  const scope = page.getByRole('navigation', { name: 'Current run scope' })
  await expect(overview.getByText('ACC', { exact: true })).toHaveAttribute('title', 'app-acc-perf-acc')
  await expect(scope.getByText('ACC', { exact: true })).toHaveAttribute('title', 'app-acc-perf-acc')
  await expect(overview.getByRole('button', { name: 'Copy application ID' })).toBeHidden()
  await overview.getByText('Technical identifiers', { exact: true }).click()
  await overview.getByRole('button', { name: 'Copy application ID' }).click()
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe('app-acc-perf-acc')
  await page.goto('/runs/run-active/phases/execution?tab=log')
  const activity = page.getByRole('region', { name: 'Activity', exact: true })
  await expect(activity).toContainText('No demo activity was captured for this phase.')
  await expect(activity).not.toContainText('No live activity for this phase yet')
  await expect(activity).not.toContainText('Activity streams in while the run is executing')
  await activity.getByRole('button', { name: 'Open Agent reasoning', exact: true }).click()
  await expect(page.getByRole('tab', { name: 'Agent reasoning', exact: true })).toBeFocused()
  await expect(page.getByRole('tab', { name: 'Agent reasoning', exact: true })).toHaveAttribute('aria-selected', 'true')
  await expect(page.getByRole('tabpanel', { name: 'Agent reasoning', exact: true }).getByRole('link', { name: /Full agent transcript/ })).toBeVisible()
})

const phases = ['story_analysis', 'test_planning', 'env_triage', 'script_scenario', 'execution', 'reporting', 'postmortem']
const application = '24fc7150-cd9c-4bfe-bc48-e6cd49e515be'

test('omitted execution has one unavailable verdict with a separate explanation', async ({ page }) => {
  const run = { thread_id: 'report-only', title: 'Report only', thread_status: 'idle', current_phase: 'reporting',
    execution_result: null, pending_gate: null, interrupts: [],
    phase_strip: [{ phase: 'reporting', status: 'succeeded' }],
    values: { phases_plan: ['reporting'], phase_results: { reporting: { status: 'succeeded' } } } }
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'omitted-execution-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = []
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'viewer', name: 'Viewer', role: 'viewer', scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'viewer', name: 'Viewer', consumer_type: 'dashboard', role: 'viewer', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/pipelines') json = { items: [], total: 0, limit: 100, offset: 0 }
    else if (path === '/v1/pipelines/report-only') json = run
    return route.fulfill({ json })
  })
  await page.goto('/runs/report-only')
  const overview = page.getByRole('region', { name: 'Run overview', exact: true })
  const acceptance = overview.getByRole('article').filter({ has: page.getByRole('heading', { name: 'Test acceptance', exact: true }) })
  await expect(acceptance.getByText('Not available', { exact: true })).toBeVisible()
  await expect(acceptance).toContainText('Execution was not selected; there is no test acceptance decision for this run.')
  await expect(page.getByText('Not applicable', { exact: true })).toHaveCount(0)
  await expect(page.locator('.run-workspace-meta').filter({ hasText: 'Test result:' })).toHaveText('Test result: Not available')
})

function reviewRun(id: string, title: string, phase: string, kind: 'prompt_review' | 'phase_review', timestamp: string) {
  const phase_strip = phases.map((name, index) => ({ phase: name, attempt: 1,
    status: name === phase ? kind === 'prompt_review' ? 'awaiting_prompt_review' : 'awaiting_output_review'
      : index < phases.indexOf(phase) ? 'succeeded' : 'pending' }))
  const pending_gate = { interrupt_id: `interrupt-${id}`, kind, phase }
  const payload = kind === 'prompt_review' ? {
    schema_version: 1, kind, phase,
    prompt: { system: 'Review the plan.', user: 'Prepare the phase.', application: null, source: { origin: 'catalog', ref: 'test_planning@v1' } },
    additional_context: '', context_packets: [], tools: [], editable: true,
    actions: ['approve', 'modify', 'skip_phase', 'abort'],
  } : {
    schema_version: 1, kind, phase, summary: 'Review the retained report.',
    result_preview: { summary: 'Retained report.', reasoning_digest: null },
    evidence: {}, artifacts: [], warnings: [], dialogue_tail: [], actions: ['approve', 'revise', 'discuss', 'abort'],
  }
  return {
    thread_id: id, title, project_id: 'project-one', app_id: application, thread_status: 'interrupted',
    current_phase: phase, phase_strip, pending_gate, created_at: timestamp, updated_at: timestamp,
    values: { phases_plan: phases, current_phase: phase, phase_results: Object.fromEntries(phase_strip.map(entry => [entry.phase, entry])) },
    interrupts: [{ ...pending_gate, payload }],
  }
}

for (const role of ['viewer', 'operator'] as const) {
  test(`queue selection announces concise context and keeps full details accessible for ${role}`, async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const first = reviewRun('first-review', 'Plan review', 'test_planning', 'prompt_review', '2026-10-17T10:00:00Z')
    const second = reviewRun('second-review', 'Report review', 'reporting', 'phase_review', '2026-10-17T11:00:00Z')
    const scopes = [{ project_id: 'project-one', app_id: null }]
    await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'queue-context-fixture'))
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
    await page.route('**/v1/**', route => {
      const path = new URL(route.request().url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Reviewer', role, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Reviewer', consumer_type: 'dashboard', role, scopes, is_unscoped: false, capabilities: {} }
      else if (path === '/v1/pipelines') json = { items: [first, second], total: 2, limit: 100, offset: 0 }
      else if (path === '/v1/pipelines/first-review') json = first
      else if (path === '/v1/pipelines/second-review') json = second
      else if (path === '/v1/catalog/applications') json = [{ id: application, project_id: 'project-one', name: 'ACC' }]
      else if (path === '/v1/catalog/environments') json = []
      else if (path.endsWith('/prompt-review')) return route.fulfill({ status: 404, json: { detail: 'No saved prompt review' } })
      return route.fulfill({ json })
    })
    await page.goto('/approvals')
    const updates = page.getByRole('status', { name: 'Run updates' })
    await expect(updates).toHaveText('Plan review. Review prompts open on Test Planning. Queue item 1 of 2.')
    const queue = page.getByRole('listbox', { name: 'Needs review queue' })
    await queue.getByRole('option', { selected: true }).press('j')
    await expect(updates).toHaveText('Report review. Review phase results open on Reporting. Queue item 2 of 2.')
    await expect(queue.getByRole('option', { selected: true })).toBeFocused()
    await expect(updates).not.toContainText('Snapshot refresh active')
    await expect(updates).not.toContainText('Story Analysis:')
    await expect(page.getByRole('button', { name: 'Story Analysis — Complete (attempt 1)', exact: true })).toBeVisible()
    await queue.getByRole('option', { selected: true }).press('k')
    await expect(updates).toHaveText('Plan review. Review prompts open on Test Planning. Queue item 1 of 2.')
    await page.getByRole('navigation', { name: 'Run sections' }).getByRole('button', { name: 'Overview', exact: true }).click()
    const overview = page.getByRole('region', { name: 'Run overview', exact: true })
    await expect(overview.getByText('ACC', { exact: true })).toHaveAttribute('title', application)
    await expect(page.getByRole('navigation', { name: 'Current run scope' }).getByText('ACC', { exact: true })).toHaveAttribute('title', application)
    expect(errors).toEqual([])
  })
}
