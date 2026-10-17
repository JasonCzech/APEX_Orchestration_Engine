import { expect, test, type Locator } from '@playwright/test'

const timestamp = '2026-10-17T12:00:00Z'
const base = {
  project_id: 'project-one', app_id: 'app-one', thread_status: 'error',
  current_phase: 'test_planning', execution_result: null,
  phase_strip: [{ phase: 'test_planning', status: 'failed', attempt: 1 }],
  created_at: timestamp, updated_at: timestamp, pending_gate: null, interrupts: [],
}
const validPrompt = {
  system: 'Preserved catalog system prompt.', phase_prompt: 'Preserved phase instructions.',
  application: null, additional_context: '', source: { origin: 'catalog' },
  updated_at: timestamp, updated_by: 'reviewer',
}
const validApplication = {
  content: 'Preserved application instructions.', source: { origin: 'catalog' },
  updated_at: timestamp, updated_by: 'reviewer',
}
const compact = {
  ...base, thread_id: 'bounded-run', title: 'Bounded evidence run',
  values: {
    projection_truncated: true, phases_plan: ['test_planning'], current_phase: 'test_planning',
    phase_results: { test_planning: { status: 'failed', attempt: 1 } },
  },
}
const drift = {
  ...base, thread_id: 'drifted-run', title: 'Drifted evidence run',
  phase_strip: [{ phase: 'story_analysis', status: 'succeeded', attempt: 1 }, ...base.phase_strip],
  values: {
    phases_plan: ['story_analysis', 'test_planning'], current_phase: 'test_planning',
    phase_results: { story_analysis: { status: 'succeeded', attempt: 1 }, test_planning: { status: 'failed', attempt: 1 } },
    engine_handle: {
      engine: 'apex_load', application_id: { unexpected: 'object' }, release_id: 'preserved-release',
      extras: { mapping_revision: { unexpected: 'object' } },
    },
    prompt_reviews: { story_analysis: validPrompt, test_planning: { ...validPrompt, system: { unexpected: 'object' } } },
    application_reviews: { 'app-one': validApplication, invalid: { ...validApplication, content: { unexpected: 'object' } } },
    dialogue: [
      { id: 'valid-dialogue', phase: 'test_planning', role: 'operator', content: 'Preserved operator discussion.', at: timestamp },
      { id: 'invalid-dialogue', phase: 'test_planning', role: 'agent', content: { unexpected: 'object' } },
    ],
  },
}
const gate = {
  ...base, thread_id: 'bounded-gate', title: 'Bounded review evidence', thread_status: 'interrupted',
  phase_strip: [{ phase: 'test_planning', status: 'awaiting_output_review', attempt: 1 }],
  values: { phases_plan: ['test_planning'], current_phase: 'test_planning', phase_results: {} },
  pending_gate: { interrupt_id: 'bounded-int', kind: 'phase_review', phase: 'test_planning' },
  interrupts: [{
    interrupt_id: 'bounded-int', kind: 'phase_review', phase: 'test_planning', payload: {
      schema_version: 1, kind: 'phase_review', phase: 'test_planning', projection_truncated: true,
      summary: 'Review the retained plan summary.', result_preview: { summary: 'Retained plan summary.', reasoning_digest: null },
      evidence: { projection_truncated: true }, artifacts: [], warnings: [], dialogue_tail: [],
      actions: ['approve', 'revise', 'discuss', 'abort'],
    },
  }],
}
const uncollected = {
  ...base, thread_id: 'uncollected-run', title: 'Execution awaiting collection', thread_status: 'interrupted',
  current_phase: 'execution', phase_strip: [{ phase: 'execution', status: 'awaiting_output_review', attempt: 1 }],
  values: {
    phases_plan: ['execution'], current_phase: 'execution', phase_results: {
      execution: { status: 'awaiting_output_review', attempt: 1,
        test_summary: { passed: false, kpis: { p95_ms: 0, error_rate: 0 } },
      },
    },
  },
}

for (const role of ['viewer', 'operator'] as const) {
  test(`bounded and drifted evidence stays truthful and readable for ${role}`, async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', error => errors.push(error.message))
    const scopes = [{ project_id: 'project-one', app_id: null }]
    await page.addInitScript(() => {
      localStorage.setItem('apex.apiKey', 'bounded-evidence-fixture')
      localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}')
    })
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
    await page.route('**/v1/**', async route => {
      const path = new URL(route.request().url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Evidence reviewer', role, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Evidence reviewer', consumer_type: 'dashboard', role, scopes, is_unscoped: false, capabilities: {} }
      else if (path === '/v1/pipelines' || path === '/v1/engine-runs') json = { items: [], total: 0, limit: 20, offset: 0 }
      else if (path === '/v1/catalog/applications' || path === '/v1/catalog/environments') json = []
      else if (path === '/v1/pipelines/bounded-run') json = compact
      else if (path === '/v1/pipelines/drifted-run') json = drift
      else if (path === '/v1/pipelines/bounded-gate') json = gate
      else if (path === '/v1/pipelines/uncollected-run') json = uncollected
      // Force the prompt UI to use the retained, independently validated snapshot.
      else if (path.endsWith('/prompt-review')) return route.fulfill({ status: 404, json: { detail: 'Prompt endpoint unavailable; inspect retained snapshot.' } })
      return route.fulfill({ json })
    })

    await page.goto('/runs/bounded-run')
    const overview = page.getByRole('region', { name: 'Run overview', exact: true })
    await expect(page.getByRole('note')).toContainText('The displayed history may be incomplete.')
    await expect(overview).toContainText('Execution evidence is unavailable in this preview.')
    await expect(overview).toContainText('The failure or stop reason is unavailable in this preview.')
    await expect(overview).toContainText('Artifact details are unavailable in this preview.')
    await expect(overview).not.toContainText('No artifacts were recorded')
    await expect(overview).not.toContainText('Not applicable')
    await page.getByRole('navigation', { name: 'Run sections' }).getByRole('link', { name: 'Workspace', exact: true }).click()
    await expect(page.getByRole('tab', { name: 'Phase details', exact: true })).toHaveAttribute('aria-selected', 'true')
    const phase = page.getByRole('region', { name: 'Phase snapshot', exact: true })
    await expect(phase).toContainText('The phase summary is unavailable in this preview.')
    await expect(phase).not.toContainText('No phase artifacts')
    await page.getByRole('tab', { name: 'Agent reasoning', exact: true }).click()
    await expect(page.getByText('Reasoning details are unavailable in this preview.', { exact: true })).toBeVisible()
    await expect(page.getByText('No reasoning details recorded for this phase.', { exact: true })).toHaveCount(0)

    await page.goto('/runs/drifted-run')
    await expect(overview).toContainText('preserved-release')
    await expect(page.getByText('schema drift', { exact: true })).toBeVisible()
    await page.getByRole('navigation', { name: 'Run sections' }).getByRole('link', { name: 'Workspace', exact: true }).click()
    await page.getByRole('tab', { name: 'Agent reasoning', exact: true }).click()
    await expect(page.getByRole('tabpanel', { name: 'Agent reasoning', exact: true })).toContainText('Preserved operator discussion.')
    await page.goto('/runs/drifted-run/phases/story_analysis?tab=details')
    await page.getByText('Prompt configuration', { exact: true }).click()
    await expect(page.locator('[data-testid="prompt-tab-editor-system"] .cm-content')).toContainText(validPrompt.system)
    await page.getByRole('tab', { name: 'Application prompt', exact: true }).click()
    await expect(page.locator('[data-testid="prompt-tab-editor-application"] .cm-content')).toContainText(validApplication.content)

    await page.goto('/runs/uncollected-run/phases/execution?tab=details')
    await expect(page.getByRole('region', { name: 'Execution workspace', exact: true })).toBeVisible()
    await expect(page.getByTestId('kpi-row')).toHaveCount(0)
    await expect(page.getByText('Test result:', { exact: false })).toContainText('Not available')

    await page.goto('/runs/bounded-gate/reviews/bounded-int')
    const review = page.getByRole('region', { name: 'Review phase results', exact: true })
    await expect(review).toContainText('Some output or discussion was omitted from this review preview')
    await expect(review).toContainText('Some agent evidence was omitted from this review preview')
    await expect(review.getByText('Reasoning details are unavailable in this preview.', { exact: true })).toBeVisible()
    await expect(phase).toContainText('The phase summary is unavailable in this preview.')
    await expect(phase).toContainText('Output source: Unavailable in this preview')
    await expect(phase).not.toContainText('No phase artifacts')
    await expect(phase).not.toContainText('No operator-readable summary has been recorded')
    await expect(page.getByRole('tabpanel', { name: 'Phase details', exact: true })).toContainText('Output source: Unavailable in this preview')
    const approve = review.getByRole('button', { name: 'Accept phase output & continue', exact: true })
    if (role === 'operator') await expect(approve).toBeEnabled()
    else await expect(approve).toHaveCount(0)
    await expect(page.getByText('Unexpected Application Error!', { exact: true })).toHaveCount(0)
    expect(errors).toEqual([])
  })
}

test('large Unicode prompt bodies stay complete and keyboard escapable for an operator', async ({ page }) => {
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const system = '始' + '界'.repeat(44_998) + '終'
  const user = '初' + '測'.repeat(44_998) + '末'
  expect(Buffer.byteLength(system + user, 'utf8')).toBeGreaterThan(256_000)
  const payload = {
    schema_version: 1, kind: 'prompt_review', phase: 'test_planning',
    prompt: { system, user, application: null, source: { origin: 'catalog', ref: 'phase/test_planning/system@v1' } },
    additional_context: '', context_packets: [], tools: [], editable: true,
    actions: ['approve', 'modify', 'skip_phase', 'abort'],
  }
  const snapshot = {
    ...base, thread_id: 'unicode-prompt', title: 'Large Unicode prompt review', thread_status: 'interrupted',
    values: { phases_plan: ['test_planning'], current_phase: 'test_planning', phase_results: {} },
    phase_strip: [{ phase: 'test_planning', status: 'awaiting_prompt_review', attempt: 1 }],
    pending_gate: { interrupt_id: 'unicode-int', kind: 'prompt_review', phase: 'test_planning' },
    interrupts: [{ interrupt_id: 'unicode-int', kind: 'prompt_review', phase: 'test_planning', payload }],
  }
  const scopes = [{ project_id: 'project-one', app_id: null }]
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'unicode-prompt-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = {}
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'operator', name: 'Prompt reviewer', role: 'operator', scopes } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'operator', name: 'Prompt reviewer', consumer_type: 'dashboard', role: 'operator', scopes, is_unscoped: false, capabilities: {} }
    else if (path === '/v1/pipelines/unicode-prompt') json = snapshot
    else if (path === '/v1/pipelines' || path === '/v1/engine-runs') json = { items: [], total: 0, limit: 20, offset: 0 }
    else if (path === '/v1/catalog/applications' || path === '/v1/catalog/environments') json = []
    return route.fulfill({ json })
  })

  // CodeMirror virtualizes long lines. Copy its entire selection through the
  // browser event contract to check every character without reading its internals
  // or replacing the user's system clipboard.
  async function copiedPrompt(editor: Locator) {
    await editor.focus()
    await editor.press('ControlOrMeta+a')
    return editor.evaluate(element => {
      const data = new DataTransfer()
      element.dispatchEvent(new ClipboardEvent('copy', { bubbles: true, cancelable: true, clipboardData: data }))
      return data.getData('text/plain')
    })
  }

  await page.goto('/runs/unicode-prompt/reviews/unicode-int')
  const review = page.getByRole('region', { name: 'Review prompts', exact: true })
  const execute = review.getByRole('button', { name: 'Execute phase', exact: true })
  const systemEditor = review.getByRole('textbox', { name: 'System prompt', exact: true })
  await expect(systemEditor).toBeVisible()
  expect(await copiedPrompt(systemEditor)).toBe(system)
  await systemEditor.press('Tab')
  await expect(systemEditor).not.toBeFocused()
  expect(await copiedPrompt(systemEditor)).toBe(system)
  await expect(execute).toBeEnabled()
  await review.getByRole('tab', { name: 'Phase prompt', exact: true }).click()
  const phaseEditor = review.getByRole('textbox', { name: 'Phase prompt', exact: true })
  await expect(phaseEditor).toBeVisible()
  expect(await copiedPrompt(phaseEditor)).toBe(user)
  await phaseEditor.press('Tab')
  await expect(phaseEditor).not.toBeFocused()
  expect(await copiedPrompt(phaseEditor)).toBe(user)
  await expect(review.getByTestId('gate-dirty-chip')).toHaveCount(0)
  await expect(review.getByRole('button', { name: 'Save edits and re-review', exact: true })).toHaveCount(0)
  await expect(execute).toBeEnabled()
  expect(errors).toEqual([])
})
