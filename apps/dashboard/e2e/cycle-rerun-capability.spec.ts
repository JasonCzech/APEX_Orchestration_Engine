import { expect, test } from '@playwright/test'

for (const role of ['operator', 'admin'] as const) {
  for (const width of [390, 1024]) {
    test(`cycle execution rerun guidance stays authoritative for ${role} at ${width}px`, async ({ page }) => {
      const scopes = [{ project_id: 'project', app_id: null }]
      const run = { thread_id: 'cycle-run', title: 'Approved cycle execution', project_id: 'project', app_id: 'app', thread_status: 'idle', current_phase: 'execution', phase_strip: [{ phase: 'execution', status: 'succeeded', attempt: 1 }], pending_gate: null, interrupts: [], rerun_blocked_reason: 'cycle_preparation_required', values: { phases_plan: ['execution'], phase_results: { execution: { status: 'succeeded', attempt: 1 } } } }
      const ordinary = { ...run, thread_id: 'ordinary-run', title: 'Ordinary pipeline', rerun_blocked_reason: null }
      let listKnowsRestriction = true
      let submissions = 0
      await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'cycle-rerun-browser-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
      await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
      await page.route('**/threads/*/runs*', route => route.fulfill({ json: [] }))
      await page.route('**/v1/**', route => {
        const path = new URL(route.request().url()).pathname
        let json: unknown = []
        if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer: { id: 'operator', name: 'Cycle operator', role, scopes } }
        else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'operator', name: 'Cycle operator', consumer_type: 'dashboard', role, scopes, is_unscoped: false, capabilities: {} }
        else if (path === '/v1/pipelines') json = { items: [{ ...run, rerun_blocked_reason: listKnowsRestriction ? run.rerun_blocked_reason : null }, ordinary], total: 2, limit: 20, offset: 0 }
        else if (path === '/v1/pipelines/cycle-run') json = run
        else if (path === '/v1/pipelines/ordinary-run') json = ordinary
        else if (path === '/v1/pipelines/ordinary-run/rerun') {
          expect(route.request().postDataJSON()).toMatchObject({ phases: ['execution'], gates_mode: 'inherit' })
          submissions += 1
          return route.fulfill({ status: 202, json: { run_id: 'ordinary-rerun' } })
        }
        else if (path.endsWith('/rerun')) { submissions += 1; return route.fulfill({ status: 409, json: { detail: 'Cycle executions require a new approved preparation' } }) }
        else if (path === '/v1/catalog/applications') json = [{ id: 'app', project_id: 'project', name: 'Checkout' }]
        else if (path.endsWith('/prompt-review')) return route.fulfill({ status: 404, json: { detail: 'No saved prompt review' } })
        return route.fulfill({ json })
      })
      await page.setViewportSize({ width, height: 844 })
      await page.goto('/runs')
      const row = page.getByTestId('runs-row-cycle-run')
      await expect(row).toContainText('Cycle executions require a new approved preparation before another launch.')
      await expect(row.getByRole('link', { name: 'Review test cycles' })).toHaveAttribute('href', '/cycles')
      await expect(row.getByRole('button', { name: 'Re-run…', exact: true })).toHaveCount(0)
      await expect(row.getByRole('button', { name: /^Run actions:/ })).toHaveCount(0)
      if (width === 390) expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)

      // A stale/older list response cannot bypass the authoritative detail check.
      listKnowsRestriction = false
      await page.reload()
      if (width === 390) await row.getByRole('button', { name: 'Re-run…', exact: true }).click()
      else {
        await row.getByRole('button', { name: /^Run actions:/ }).click()
        await page.getByRole('menuitem', { name: 'Re-run…', exact: true }).click()
      }
      const dialog = page.getByRole('dialog', { name: 'Re-run phases' })
      await expect(dialog).toContainText('Cycle executions require a new approved preparation before another launch.')
      await expect(dialog.getByRole('button', { name: 'Start phases' })).toBeDisabled()
      await expect(dialog.getByRole('group', { name: 'Phases to run' })).toHaveCount(0)
      expect(submissions).toBe(0)
      await dialog.getByRole('button', { name: 'Cancel', exact: true }).click()

      await page.goto('/runs/cycle-run/phases/execution')
      await expect(page.getByRole('link', { name: 'Review test cycles' })).toBeVisible()
      await expect(page.getByRole('button', { name: 'Re-run selected plan' })).toHaveCount(0)
      await expect(page.getByRole('button', { name: 'Re-run options' })).toHaveCount(0)
      expect(submissions).toBe(0)

      await page.goto('/runs/ordinary-run/phases/execution')
      await page.getByRole('button', { name: 'Re-run selected plan' }).click()
      const ordinaryDialog = page.getByRole('dialog', { name: 'Re-run phases' })
      await expect(ordinaryDialog.getByRole('button', { name: 'Execution', exact: true })).toHaveAttribute('aria-pressed', 'true')
      await ordinaryDialog.getByRole('button', { name: 'Start phases' }).click()
      await expect(ordinaryDialog).toHaveCount(0)
      expect(submissions).toBe(1)
    })
  }
}
