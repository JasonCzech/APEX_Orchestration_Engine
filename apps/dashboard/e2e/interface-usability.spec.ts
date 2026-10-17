import { expect, test } from '@playwright/test'

test('compact grid phase progress is one labelled graphic and detail phases remain keyboard actions', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/runs')
  const row = page.locator('.runs-row').first()
  const progress = row.getByRole('img', { name: /^Phase progress\./ })
  await expect(progress).toBeVisible()
  for (const name of ['Story Analysis', 'Test Planning', 'Env Triage', 'Script & Scenario', 'Execution', 'Reporting', 'Postmortem']) {
    await expect(progress).toHaveAccessibleName(new RegExp(name))
  }
  await expect(progress.locator(':scope > span[aria-hidden="true"]')).toHaveCount(7)
  await expect(progress.locator('button, a, [tabindex]')).toHaveCount(0)
  const target = row.getByRole('button', { name: /^Open .* details$/ })
  await target.focus()
  await target.press('Enter')
  const phases = page.getByRole('group', { name: 'Phase progress', exact: true })
  const execution = phases.getByRole('button', { name: /^Execution — / })
  await expect(execution).toBeVisible()
  await execution.focus()
  await execution.press('Enter')
  await expect(execution).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByRole('region', { name: 'Execution workspace', exact: true })).toBeVisible()
})

test('retained tool statuses and reasoning provenance stay distinct and explicitly labelled', async ({ page }) => {
  const statuses = [
    ['ok', 'success'], ['success', 'success'], ['completed', 'success'],
    ['error', 'danger'], ['failed', 'danger'], ['timeout', 'danger'], ['denied', 'danger'], ['cancelled', 'danger'],
    ['running', 'accent'], ['pending', 'accent'], ['unknown', 'neutral'], ['future_status', 'neutral'], [undefined, 'neutral'],
  ] as const
  const entry = {
    phase: 'execution', status: 'succeeded', attempt: 1, provenance: 'llm',
    reasoning_digest: 'Inspected the retained tool evidence.',
    resolved_prompt: { system: 'System instructions.', user: 'User instructions.' },
    resolved_prompt_source: { origin: 'run_override', ref: 'execution@v1', editor: 'reviewer-one' },
    transcript_ref: { id: 'full-transcript', kind: 'transcript', media_type: 'text/plain', name: 'Full transcript' },
    tool_calls: statuses.map(([status], index) => ({ id: `tool-${index}`, tool: `Tool ${index}`, ...(status ? { status } : {}), duration_ms: index })),
  }
  const run = {
    thread_id: 'tool-status-fixture', title: 'Tool evidence', thread_status: 'idle', current_phase: 'execution',
    phase_strip: [{ phase: 'execution', status: 'succeeded', attempt: 1 }], pending_gate: null,
    values: { phases_plan: ['execution'], current_phase: 'execution', phase_results: { execution: entry },
      dialogue: [{ id: 'dialogue-one', phase: 'execution', role: 'operator', content: 'Please inspect the tool evidence.' }] },
    interrupts: [],
  }
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'tool-status-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = {}
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Reviewer', role: 'viewer', scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Reviewer', consumer_type: 'dashboard', role: 'viewer', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/pipelines/tool-status-fixture') json = run
    else if (path === '/v1/pipelines') json = { items: [], total: 0, limit: 100, offset: 0 }
    else if (path === '/v1/catalog/applications' || path === '/v1/catalog/environments') json = []
    else if (path.endsWith('/prompt-review')) return route.fulfill({ status: 404, json: { detail: 'No saved prompt review' } })
    return route.fulfill({ json })
  })
  await page.goto('/runs/tool-status-fixture/phases/execution?tab=reasoning')
  const reasoning = page.getByRole('tabpanel', { name: 'Agent reasoning', exact: true })
  for (const heading of ['Reasoning digest', 'Tool calls', 'Resolved prompt', 'Operator dialogue']) {
    await expect(reasoning.getByRole('heading', { name: heading, exact: true })).toBeVisible()
  }
  const rows = reasoning.getByTestId('output-tool-calls').getByRole('listitem')
  await expect(rows).toHaveCount(statuses.length)
  for (let index = 0; index < statuses.length; index += 1) {
    const [status, tone] = statuses[index]!
    const badge = rows.nth(index).locator('.status-badge')
    await expect(badge).toHaveText(status ?? 'Not recorded')
    await expect(badge).toHaveClass(new RegExp(`\\b${tone}\\b`))
  }
  await expect(reasoning.getByRole('link', { name: 'Full agent transcript (attempt 1)' })).toHaveAttribute('href', '/runs/tool-status-fixture/artifacts/full-transcript')
  await expect(reasoning.getByTestId('prompt-provenance')).toContainText('reviewer-one')
  await expect(reasoning.getByRole('textbox', { name: 'System prompt', exact: true })).toBeVisible()
  await expect(reasoning.getByRole('textbox', { name: 'User prompt', exact: true })).toBeVisible()
})

test('consumer deletion requires its exact name before the irreversible request', async ({ page }) => {
  let deleted = false
  const consumer = { id: 'ci-review-fixture', name: 'CI reviewer', role: 'viewer', consumer_type: 'headless',
    scopes: [], enabled: true, key_fingerprint: '01234567', created_at: '2026-10-17T10:00:00Z', updated_at: '2026-10-17T10:00:00Z' }
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'delete-consumer-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = {}
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'admin', name: 'Admin', role: 'admin', scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'admin', name: 'Admin', consumer_type: 'dashboard', role: 'admin', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/admin/consumers') json = deleted ? [] : [consumer]
    else if (path === '/v1/admin/consumers/ci-review-fixture' && route.request().method() === 'DELETE') {
      deleted = true
      return route.fulfill({ status: 204 })
    }
    else if (path === '/v1/pipelines') json = { items: [], total: 0, limit: 20, offset: 0 }
    return route.fulfill({ json })
  })
  await page.goto('/admin/consumers')
  await page.getByRole('button', { name: 'Consumer actions: CI reviewer' }).click()
  await page.getByRole('menuitem', { name: 'Delete…', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Delete consumer CI reviewer' })
  const confirm = dialog.getByRole('button', { name: 'Delete consumer', exact: true })
  await expect(confirm).toBeDisabled()
  const input = dialog.getByRole('textbox', { name: 'Type CI reviewer to confirm' })
  await input.fill('ci reviewer')
  await expect(confirm).toBeDisabled()
  expect(deleted).toBe(false)
  await input.fill('CI reviewer')
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect(dialog).toBeHidden()
  expect(deleted).toBe(true)
})
