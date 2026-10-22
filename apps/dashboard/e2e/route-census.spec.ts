import { expect, test } from '@playwright/test'

/** Every declared route, including redirects and explicit unconfigured states.
 * This is a route/DOM contract census; workflow specs exercise actions deeply. */
const routes = [
  ['/', 'Test operations dashboard'],
  ['/approvals', 'Review', 'review-selection'],
  ['/approvals/run-prompt-review/int-prompt', 'Review', 'review-redirect'],
  ['/cycles', 'Test cycles', 'unconfigured'],
  ['/cycles/census-cycle', 'Cycle workspace', 'unconfigured'],
  ['/runs', 'Tests'],
  ['/runs/new', 'New test'],
  ['/runs/compare?ids=run-baseline-a,run-baseline-b', 'Compare runs'],
  ['/runs/run-baseline-a', 'Run detail'],
  ['/runs/run-prompt-review/reviews/int-prompt', 'Review'],
  ['/runs/run-active/phases/execution', 'Phase detail'],
  ['/runs/run-baseline-a/timeline', 'Run timeline'],
  ['/runs/run-baseline-a/artifacts/exec-report', 'Artifact viewer'],
  ['/prompts', 'Prompts'],
  ['/prompts/application/app-acc-perf-acc', 'Prompt'],
  ['/prompts/application/app-acc-perf-acc/versions/1', 'Prompt version'],
  ['/prompts/application/app-acc-perf-acc/playground', 'Template preview'],
  ['/golden-configs', 'Test presets', 'redirect'],
  ['/golden-configs/asst-release', 'Test preset', 'redirect'],
  ['/test-presets', 'Test presets'],
  ['/test-presets/asst-release', 'Test preset'],
  ['/work-items', 'Tickets'],
  ['/work-items/saved', 'Saved queries'],
  ['/work-items/jira/PERF-241?connection_id=conn-jira', 'Ticket'],
  ['/environments', 'Applications & environments'],
  ['/environments/env-acc-perf-acc-perfhyper2', 'Environment'],
  ['/context', 'Knowledge & documents'],
  ['/evidence', 'Evidence'],
  ['/analytics', 'Agent analytics'],
  ['/logs', 'Logs'],
  ['/apex-load', 'APEX Load'],
  ['/admin/connections', 'Connections'],
  ['/admin/connections/conn-jira', 'Connection'],
  ['/admin/consumers', 'API consumers'],
  ['/admin/consumers/cons-admin', 'Consumer'],
  ['/admin/system', 'System status'],
  ['/settings', 'Settings'],
  ['/census-unknown-route', 'Not found', 'not-found'],
] as const

test('all declared routes expose heading, tab-panel and compact-target contracts in the production bundle', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  const census: Array<Record<string, unknown>> = []
  for (const [path, title, disposition = 'loaded'] of routes) {
    await test.step(`${path} (${disposition})`, async () => {
      await page.goto(path)
      if (disposition === 'review-selection') await expect(page).toHaveURL(/\/runs\/[^/]+\/reviews\/[^/?]+\?source=queue$/)
      if (disposition === 'review-redirect') await expect(page).toHaveURL(/\/runs\/run-prompt-review\/reviews\/int-prompt\?source=queue$/)
      await expect(page.getByRole('heading', { level: 1, name: title, exact: true })).toBeVisible()
      await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1)
      // These route-level toolbar titles duplicated the shell heading. Nested
      // pane headings (for example the evidence results list) remain useful.
      if (path === '/evidence') await expect(page.locator('.ctx-page > .ctx-toolbar .ctx-page-title')).toHaveCount(0)
      if (path === '/work-items') await expect(page.locator('.wi-page > .wi-toolbar .wi-page-title')).toHaveCount(0)
      await expect(page.getByRole('main').locator('[aria-busy="true"]')).toHaveCount(0)
      if (disposition === 'unconfigured') await expect(page.getByText('The cycle workspace is not enabled on this server.')).toBeVisible()
      if (disposition === 'redirect') await expect(page).toHaveURL(/\/test-presets(?:\/asst-release)?$/)
      if (disposition === 'not-found') await expect(page.getByRole('link', { name: 'Go to Home', exact: true })).toBeVisible()

      const tabs = page.getByRole('tab')
      for (let index = 0; index < await tabs.count(); index += 1) {
        const tab = tabs.nth(index)
        await expect(tab).toHaveAttribute('aria-controls', /.+/)
        const panelId = await tab.getAttribute('aria-controls')
        const tabId = await tab.getAttribute('id')
        const panel = page.locator(`[id=${JSON.stringify(panelId)}]`)
        await expect(panel).toHaveAttribute('role', 'tabpanel')
        await expect(panel).toHaveAttribute('aria-labelledby', tabId!)
        await expect(tab).toHaveAttribute('tabindex', await tab.getAttribute('aria-selected') === 'true' ? '0' : '-1')
      }
      await expect(page.locator('.phase-strip--sm button, .phase-strip--sm a, .phase-strip--sm [tabindex]')).toHaveCount(0)
      census.push({ route: path, resolvedUrl: new URL(page.url()).pathname, disposition,
        headings: await page.getByRole('heading').allTextContents(), tabs: await tabs.count() })
    })
  }
  expect(errors).toEqual([])
  await testInfo.attach('route-census.json', { body: JSON.stringify(census, null, 2), contentType: 'application/json' })
})
