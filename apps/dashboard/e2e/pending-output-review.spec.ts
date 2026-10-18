import { expect, test, type Page } from '@playwright/test'

function run(id: string, title: string, updatedAt: string) {
  const payload = { schema_version: 1, kind: 'phase_review', phase: 'execution',
    summary: `${title}: engine evidence is ready for review.`, result_preview: { summary: 'Collected synthetic metrics.' },
    artifacts: [{ id: 'results-json', kind: 'engine_results', name: 'results.json' }],
    warnings: [], dialogue_tail: [], actions: ['approve', 'revise', 'discuss', 'abort'] }
  return { thread_id: id, title, project_id: 'demo', app_id: 'app-acc', thread_status: 'interrupted', current_phase: 'execution', updated_at: updatedAt,
    phase_strip: [{ phase: 'execution', status: 'awaiting_output_review', attempt: 1 }],
    pending_gate: { interrupt_id: `gate-${id}`, kind: 'phase_review', phase: 'execution' },
    values: { phases_plan: ['execution'], current_phase: 'execution', phase_results: { execution: { status: 'pending' } } },
    interrupts: [{ interrupt_id: `gate-${id}`, kind: 'phase_review', phase: 'execution', payload }] }
}

const first = run('pending-output-1', 'Pending execution', '2026-10-18T10:00:00Z')
const second = run('pending-output-2', 'Second execution', '2026-10-18T10:01:00Z')
async function fixture(page: Page) {
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'pending-output-review-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = {}
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Reviewer', role: 'operator', scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Reviewer', consumer_type: 'dashboard', role: 'operator', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/pipelines') json = { items: [first, second], total: 2, limit: 100, offset: 0 }
    else if (path === `/v1/pipelines/${first.thread_id}`) json = first
    else if (path === `/v1/pipelines/${second.thread_id}`) json = second
    else if (path === '/v1/catalog/applications') json = [{ id: 'app-acc', project_id: 'demo', name: 'ACC' }]
    else if (path === '/v1/catalog/projects' || path === '/v1/catalog/environments') json = []
    else if (path.endsWith('/prompt-review')) return route.fulfill({ status: 404, json: { detail: 'No saved prompt review' } })
    return route.fulfill({ json })
  })
}

test('root-only pending output supplies phase evidence and an untimed review in the timeline', async ({ page }) => {
  await fixture(page)
  await page.goto(`/runs/${first.thread_id}/phases/execution?tab=details`)
  const snapshot = page.getByRole('region', { name: 'Phase snapshot', exact: true })
  await expect(snapshot).toContainText('Pending execution: engine evidence is ready for review.')
  await expect(snapshot).toContainText('1 phase artifact')
  await expect(snapshot).toContainText('Not recorded while awaiting the decision')
  await expect(snapshot).toContainText('Not recorded in this review')
  await expect(snapshot).toContainText('this output has not been accepted')
  await expect(snapshot).not.toContainText('No phase artifacts')
  const details = page.getByRole('tabpanel', { name: 'Phase details', exact: true })
  await expect(details.getByRole('link', { name: /results.json/ })).toHaveAttribute('href', `/runs/${first.thread_id}/artifacts/results-json`)
  const navigation = page.getByRole('navigation', { name: 'Run sections' })
  await expect(navigation.getByRole('link', { name: 'Workspace', exact: true })).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)')
  await navigation.getByRole('link', { name: 'Timeline', exact: true }).click()
  const timeline = page.getByRole('region', { name: 'Run timeline' })
  await expect(timeline).toContainText('Execution output awaiting review')
  await expect(timeline).toContainText('Review time not recorded')
  await expect(timeline).toContainText('Pending execution: engine evidence is ready for review.')
  await expect(timeline).not.toContainText('Execution Complete')
})

test('narrow review queue starts collapsed and keyboard triage expands and focuses its selected option', async ({ page }) => {
  await page.setViewportSize({ width: 768, height: 900 })
  await fixture(page)
  await page.goto('/approvals')
  const toggle = page.getByRole('button', { name: /Show pending reviews \(2\)/ })
  await expect(toggle).toBeVisible()
  await expect(toggle).toHaveAttribute('aria-expanded', 'false')
  await expect(page.getByRole('listbox', { name: 'Needs review queue' })).toBeHidden()
  await expect(page.getByRole('region', { name: 'Review workspace' })).toBeVisible()
  await page.keyboard.press('j')
  const queue = page.getByRole('listbox', { name: 'Needs review queue' })
  await expect(queue).toBeVisible()
  const selected = queue.getByRole('option', { name: /^Second execution\./ })
  await expect(selected).toHaveAttribute('aria-selected', 'true')
  await expect(selected).toBeFocused()
  await page.getByRole('button', { name: /Hide pending reviews/ }).click()
  await expect(queue).toBeHidden()
  await expect(page.getByRole('region', { name: 'Review workspace' })).toBeVisible()
})
