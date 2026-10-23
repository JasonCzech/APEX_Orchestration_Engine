import { expect, test } from '@playwright/test'

for (const profile of ['admin', 'operator'] as const) {
  test(`new-test workload, environment readiness and retry for ${profile}`, async ({ page }) => {
    const scopes = profile === 'operator' ? [{ project_id: 'workflow', app_id: null }] : []
    const environment = { id: 'env', application_id: 'app', name: 'Unapproved staging', base_url: 'https://staging.example', target_approved: false, target_version: 0, options: {}, hosts: [], kind: 'staging' }
    const launches: Record<string, unknown>[] = []
    const readinessRequests: Record<string, unknown>[] = []
    const drafts = new Map<string, Record<string, unknown>>()
    await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'workflow-fixture'))
    await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
    await page.route('**/assistants/search', route => route.fulfill({ json: [] }))
    await page.route('**/v1/**', async route => {
      const request = route.request()
      const path = new URL(request.url()).pathname
      let json: unknown = {}
      if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Workflow reviewer', role: profile, scopes } }
      else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Workflow reviewer', consumer_type: 'dashboard', role: profile, scopes, is_unscoped: !scopes.length, capabilities: {} }
      else if (path === '/v1/catalog/applications') json = [{ id: 'app', project_id: 'workflow', name: 'Checkout', archived_at: null }]
      else if (path === '/v1/catalog/projects') json = []
      else if (path === '/v1/catalog/environments') json = [environment]
      else if (path === '/v1/catalog/environments/env') json = environment
      else if (path === '/v1/pipelines/execution-readiness') {
        const body = request.postDataJSON()
        readinessRequests.push(body)
        // The operator's persisted execution connection needs the primary
        // system's application; the admin's development simulator does not.
        const needsApplication = profile === 'operator' && !body.app_id
        json = { execution_selected: body.phases.includes('execution'), configuration_valid: !needsApplication, requires_application: profile === 'operator',
          detail: needsApplication ? 'Select an application for the configured execution connection' : null }
      }
      else if (path === '/v1/drafts' && request.method() === 'POST') {
        json = { ...request.postDataJSON(), id: 'draft', created_at: '2026-10-18T00:00:00Z', updated_at: '2026-10-18T00:00:00Z' }
        drafts.set('draft', json as Record<string, unknown>)
      } else if (path === '/v1/drafts') json = [...drafts.values()]
      else if (path === '/v1/drafts/draft' && request.method() === 'PUT') { json = { ...drafts.get('draft'), ...request.postDataJSON() }; drafts.set('draft', json as Record<string, unknown>) }
      else if (path === '/v1/drafts/draft') json = drafts.get('draft') ?? {}
      else if (path === '/v1/pipelines' && request.method() === 'POST') {
        launches.push(request.postDataJSON())
        return route.fulfill(launches.length === 1 ? { status: 422, json: { title: 'Selected environment target changed', status: 422 } } : { status: 202, json: { thread_id: 'launched', run_id: 'run', stream_url: '/stream' } })
      } else if (path === '/v1/pipelines/launched') json = { thread_id: 'launched', title: 'Reviewed workload', thread_status: 'idle', project_id: 'workflow', app_id: 'app', phase_strip: [], pending_gate: null, values: { phase_results: {}, phases_plan: [] }, interrupts: [] }
      else if (path === '/v1/prompts') json = [
        { id: 'system', namespace: 'system', key: 'default', phase: null, is_default: true, selection_enabled: true, active_version: { id: 'system-v1', version: 1 } },
        ...['story_analysis', 'test_planning', 'env_triage', 'script_scenario', 'execution', 'reporting', 'postmortem'].map(phase => ({ id: phase, namespace: 'phase', key: `${phase}/system`, phase, is_default: true, selection_enabled: true, active_version: { id: `${phase}-v1`, version: 1 } })),
        // Application context is offered only for the systems in this test's scope.
        { id: 'checkout-context', namespace: 'application', key: 'checkout-context', application_id: 'app', is_shared: false, selection_enabled: true, active_version: { id: 'checkout-context-v1', version: 1 } },
        { id: 'billing-context', namespace: 'application', key: 'billing-context', application_id: 'other-app', is_shared: false, selection_enabled: true, active_version: { id: 'billing-context-v1', version: 1 } },
      ]
      else if (path.startsWith('/v1/prompts/')) json = { id: path.split('/').at(-1), version: 1, content: 'Default prompt instructions' }
      else if (path === '/v1/pipelines/analysis/connections') json = []
      else json = { items: [], total: 0, limit: 100, offset: 0 }
      return route.fulfill({ json })
    })
    await page.setViewportSize({ width: 1440, height: 900 })
    await page.goto('/runs/new')
    await page.getByLabel('Title', { exact: true }).fill('Reviewed workload')
    await page.getByLabel('Request', { exact: true }).fill('Exercise the checkout request under reviewed load.')
    // Scope is a system topology: one backend card with its own catalog fields.
    await page.getByRole('button', { name: 'Add backend', exact: true }).click()
    await page.getByLabel('Project', { exact: true }).selectOption('workflow')
    // Choosing the primary before its application lets readiness run for the
    // operator, whose execution connection then asks for that application.
    if (profile === 'operator') await page.getByRole('radio', { name: 'Primary system under test', exact: true }).check()
    await page.getByRole('tab', { name: 'Review', exact: true }).click()
    const issueName = profile === 'operator' ? /Select an application for the configured execution connection/ : /every participant/
    const missing = page.getByTestId('review-issues').getByRole('button', { name: issueName })
    await expect(missing).toBeVisible()
    await expect(page.getByRole('button', { name: 'Launch test', exact: true })).toBeDisabled()
    const application = page.getByLabel('Application', { exact: true })
    const expectVisibleApplicationFocus = async () => {
      await expect(application).toBeFocused()
      await expect.poll(() => application.evaluate(element => {
        const field = element.getBoundingClientRect()
        const footer = document.querySelector('.wizard-footer')!.getBoundingClientRect()
        return field.top >= 0 && field.bottom + 4 <= footer.top &&
          document.elementFromPoint(field.left + field.width / 2, field.top + field.height / 2) === element
      })).toBe(true)
    }
    // Readiness links land on the first missing field, visible above the footer.
    await missing.click()
    await expectVisibleApplicationFocus()
    // Same-step issue links must reveal the target too, including a narrow
    // viewport where the footer disclosure opens above the footer.
    await page.setViewportSize({ width: 390, height: 800 })
    await page.locator('#main-content').evaluate(element => { element.scrollTop = 0 })
    await page.locator('.wizard-footer-issues > summary').click()
    await page.getByRole('list', { name: 'Items needing attention' }).getByRole('button', { name: issueName }).click()
    await expectVisibleApplicationFocus()
    await page.setViewportSize({ width: 1440, height: 900 })
    await application.selectOption('app')
    await page.getByLabel('Environment', { exact: true }).selectOption('env')
    await expect(page.getByLabel('Environment', { exact: true }).locator('option:checked')).toHaveText('Unapproved staging · Target not approved')
    await page.getByRole('radio', { name: 'Primary system under test', exact: true }).check()
    await page.getByRole('radio', { name: 'Traffic entry', exact: true }).check()
    expect(await page.getByLabel('Title', { exact: true }).evaluate(element => element.getBoundingClientRect().width)).toBeLessThanOrEqual(640)
    await page.getByRole('tab', { name: 'Config', exact: true }).click()
    await page.getByRole('radio', { name: /^APEX Load/ }).click()
    await page.getByRole('tab', { name: 'Review', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Launch test', exact: true })).toBeDisabled()
    await expect(page.getByRole('link', { name: 'Review environment target' })).toHaveAttribute('href', '/environments/env')
    await page.getByRole('tab', { name: 'Config', exact: true }).click()
    await page.getByRole('radio', { name: /^Simulated/ }).click()
    await page.getByRole('spinbutton', { name: 'VUsers', exact: true }).fill('300')
    await page.getByRole('spinbutton', { name: 'Maximum error rate (%)' }).fill('0.5')
    await page.getByRole('spinbutton', { name: 'Minimum throughput (TPS)' }).fill('85')
    await page.getByRole('tab', { name: 'Tickets', exact: true }).click()
    await expect(page).toHaveURL(/step=tickets/)
    await page.getByRole('tab', { name: 'Knowledge', exact: true }).click()
    await expect(page).toHaveURL(/step=knowledge/)
    await page.getByRole('tab', { name: 'Prompts', exact: true }).click()
    await page.getByRole('tablist', { name: 'Prompt selection' }).getByRole('tab', { name: /Application context/ }).click()
    const appContext = page.getByRole('combobox', { name: 'Add Application context' })
    await expect(appContext.locator('option', { hasText: 'checkout-context' })).toHaveCount(1)
    await expect(appContext.locator('option', { hasText: 'billing-context' })).toHaveCount(0)
    await page.getByRole('button', { name: 'Review test', exact: true }).click()
    await page.getByRole('button', { name: 'Launch test', exact: true }).click()
    await expect(page.getByRole('alert')).toContainText('Selected environment target changed')
    await expect(page.getByRole('alert')).toContainText('Retrying unchanged inputs on this page')
    await page.getByRole('button', { name: 'Retry launch', exact: true }).click()
    await expect(page).toHaveURL(/\/runs\/launched$/)
    expect(readinessRequests.at(-1)).toMatchObject({ project_id: 'workflow', app_id: 'app', engine: 'sim' })
    expect(launches).toHaveLength(2)
    expect(launches[1]).toEqual(launches[0])
    expect(launches[0]?.configurable).toMatchObject({ environment_id: 'env', engine: 'sim', load_test: { vusers: 300, slas: { error_rate: 0.005, tps_avg: 85 } } })
    expect((launches[0]?.configurable as Record<string, unknown>).phases).toEqual(['story_analysis', 'test_planning', 'env_triage', 'script_scenario', 'execution', 'reporting', 'postmortem'])
  })
}
