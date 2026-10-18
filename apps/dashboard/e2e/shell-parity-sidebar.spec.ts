import { expect, test, type Page } from '@playwright/test'

async function connectedShell(page: Page, role: 'viewer' | 'operator' | 'admin') {
  await page.addInitScript(() => localStorage.setItem('apex.apiKey', 'sidebar-fixture'))
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', route => {
    const path = new URL(route.request().url()).pathname
    const consumer = { id: 'shell-user', name: 'Shell user', role, scopes: [] }
    if (path === '/v1/system/info') return route.fulfill({ json: {
      name: 'APEX Orchestration', version: 'test', environment: 'test', features: { cycles: true }, limits: { max_context_packets: 32 }, consumer,
    } })
    if (path === '/v1/auth/me') return route.fulfill({ json: {
      principal_kind: 'api_consumer', principal_id: consumer.id, consumer_type: 'dashboard', ...consumer, is_unscoped: true, capabilities: {},
    } })
    if (path === '/v1/pipelines') return route.fulfill({ json: { items: [{
      thread_id: 'sidebar-review', title: 'Pending review', project_id: 'shell-project', app_id: null,
      thread_status: 'interrupted', current_phase: 'test_planning', phase_strip: [],
      pending_gate: { interrupt_id: 'sidebar-interrupt', kind: 'prompt_review', phase: 'test_planning' },
      updated_at: '2026-10-17T19:00:00Z',
    }], total: 1, limit: 100, offset: 0 } })
    return route.fulfill({ json: [] })
  })
}

for (const role of ['viewer', 'operator', 'admin'] as const) {
  test(`desktop rail keeps navigation accessible and expansion stable for ${role}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await connectedShell(page, role)
    await page.goto('/settings')
    const main = page.locator('#main-content')
    const rail = page.getByTestId('sidebar')
    const nav = rail.getByRole('navigation', { name: 'Primary' })
    await expect(rail).toHaveCSS('width', '64px')
    for (const name of ['New test', 'Home', 'Tickets', 'Test cycles', 'Applications & environments',
      'Knowledge & documents', 'Test presets', 'Prompts', 'Evidence', 'Agent analytics', 'Logs', 'Settings']) {
      await expect(nav.getByRole('link', { name, exact: true })).toBeVisible()
    }
    for (const name of ['Connections', 'API consumers', 'System status']) {
      await expect(nav.getByRole('link', { name, exact: true })).toHaveCount(role === 'admin' ? 1 : 0)
    }
    // The badge is part of its destination's accessible name while collapsed.
    await expect(nav.getByRole('link', { name: /^Tests/ })).toHaveAccessibleName('Tests 1 test needs review')
    const tests = nav.getByRole('link', { name: /^Tests/ })
    await expect(tests.getByTestId('tests-review-nav-badge')).toBeVisible()
    await tests.focus()
    await expect(tests).toBeFocused()

    const initialBounds = await main.boundingBox()
    const expand = rail.getByRole('button', { name: 'Expand sidebar' })
    await expand.click()
    const dialog = page.getByRole('dialog', { name: 'Navigation', exact: true })
    await expect(dialog).toHaveCSS('width', '240px')
    expect(await main.boundingBox()).toEqual(initialBounds)
    const first = dialog.getByRole('link', { name: 'New test', exact: true })
    const last = dialog.getByRole('button', { name: 'Collapse sidebar', exact: true })
    await expect(first).toBeFocused()
    await page.keyboard.press('Shift+Tab')
    await expect(last).toBeFocused()
    await page.keyboard.press('Tab')
    await expect(first).toBeFocused()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
    await expect(expand).toBeFocused()
    await page.keyboard.press('Tab')
    expect(await rail.evaluate(element => element.contains(document.activeElement))).toBe(false)

    // Clicking an underlying tab dismisses the overlay without activating it.
    const connection = page.getByRole('tab', { name: 'Connection & session', exact: true })
    const target = await connection.boundingBox()
    expect(target).not.toBeNull()
    await expand.click()
    await page.mouse.click(target!.x + target!.width / 2, target!.y + target!.height / 2)
    await expect(dialog).toHaveCount(0)
    await expect(expand).toBeFocused()
    await expect(connection).toHaveAttribute('aria-selected', 'false')
    expect(await main.boundingBox()).toEqual(initialBounds)

    // The blank brand area is a pointer convenience, with the same focus return.
    await rail.locator('.sidebar-brand').click()
    await expect(dialog).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(expand).toBeFocused()
    await nav.getByRole('link', { name: 'Settings', exact: true }).click()
    await expect(dialog).toHaveCount(0)
  })
}

for (const height of [800, 900, 1000]) {
  test(`Settings and the account footer never overlap at ${height}px`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height })
    await connectedShell(page, 'admin')
    await page.goto('/settings')
    const rail = page.getByTestId('sidebar')
    const settings = rail.getByRole('link', { name: 'Settings', exact: true })
    await expect(settings).toBeVisible()
    const assertClearFooter = async (container: ReturnType<Page['locator']>) => {
      const link = container.getByRole('link', { name: 'Settings', exact: true })
      const bounds = await link.boundingBox()
      const footer = await container.locator('.sidebar-footer').boundingBox()
      expect(bounds).not.toBeNull()
      expect(footer).not.toBeNull()
      expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(footer!.y)
      // Rect checks alone miss overlaid siblings: sample the actual hit target.
      for (const fraction of [0.1, 0.5, 0.9]) {
        expect(await link.evaluate((element, part) => {
          const rect = element.getBoundingClientRect()
          const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height * part)
          return hit === element || element.contains(hit)
        }, fraction)).toBe(true)
      }
      const status = container.getByRole('link', { name: 'System status', exact: true })
      await status.focus()
      await expect(status).toBeInViewport()
      expect(await status.locator('path').getAttribute('d')).not.toBe(await link.locator('path').getAttribute('d'))
    }
    await assertClearFooter(rail)
    await settings.click()
    await expect(page.getByRole('dialog', { name: 'Navigation', exact: true })).toHaveCount(0)

    // Empty rail padding exposes the labelled overlay, not just the brand area.
    await rail.locator('.sidebar-nav-items').click({ position: { x: 2, y: 2 } })
    const overlay = page.getByRole('dialog', { name: 'Navigation', exact: true })
    await expect(overlay).toBeVisible()
    await assertClearFooter(overlay)
    await page.keyboard.press('Escape')
    await expect(rail.getByRole('button', { name: 'Expand sidebar' })).toBeFocused()
  })
}

for (const preference of ['true', 'false']) {
  test(`desktop sidebar honors stored collapsed preference ${preference}`, async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.addInitScript(value => localStorage.setItem('apex.sidebarCollapsed', value), preference)
    await page.goto('/settings')
    await expect(page.getByTestId('sidebar')).toHaveCSS('width', '64px')
    const dialog = page.getByRole('dialog', { name: 'Navigation', exact: true })
    await expect(dialog).toHaveCount(preference === 'false' ? 1 : 0)
    if (preference === 'false') {
      await page.keyboard.press('Escape')
      await expect(page.getByRole('button', { name: 'Expand sidebar' })).toBeFocused()
      expect(await page.evaluate(() => localStorage.getItem('apex.sidebarCollapsed'))).toBe('true')
    }
  })
}

test('mobile navigation remains a drawer and does not change the desktop preference', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 })
  await page.addInitScript(() => localStorage.setItem('apex.sidebarCollapsed', 'false'))
  await page.goto('/settings')
  const open = page.getByRole('button', { name: 'Open navigation', exact: true })
  await expect(open).toBeVisible()
  await expect(page.getByTestId('sidebar')).toHaveCount(0)
  const initialBounds = await page.locator('#main-content').boundingBox()
  await open.click()
  const dialog = page.getByRole('dialog', { name: 'Navigation', exact: true })
  await expect(dialog.getByRole('link', { name: 'Settings', exact: true })).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Close navigation', exact: true })).toBeFocused()
  await expect(dialog.getByRole('button', { name: 'Collapse sidebar' })).toHaveCount(0)
  expect(await page.locator('#main-content').boundingBox()).toEqual(initialBounds)
  await page.keyboard.press('Escape')
  await expect(open).toBeFocused()
  expect(await page.evaluate(() => localStorage.getItem('apex.sidebarCollapsed'))).toBe('false')
})

test('desktop navigation preserves a dirty gate through its navigation guard', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.goto('/approvals')
  const promptRow = page.getByRole('option').filter({ hasText: 'PI 26.3 workload prompt review' })
  await promptRow.click()
  const editor = page.getByRole('region', { name: 'Review prompts' }).getByRole('textbox', { name: 'System prompt', exact: true })
  await expect(editor).toBeVisible()
  const original = await editor.innerText()
  const draft = `${original}\nKeep this sidebar navigation draft.`
  await editor.fill(draft)
  const expand = page.getByRole('button', { name: 'Expand sidebar', exact: true })
  await expand.click()
  await page.getByRole('dialog', { name: 'Navigation', exact: true }).getByRole('link', { name: 'Prompts', exact: true }).click()
  const guard = page.getByRole('dialog', { name: 'Unsaved review edits', exact: true })
  await expect(guard).toBeVisible()
  const stay = guard.getByRole('button', { name: 'Continue reviewing', exact: true })
  await expect(stay).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(guard.getByRole('button', { name: 'Leave and keep draft', exact: true })).toBeFocused()
  await stay.click()
  await expect(guard).toHaveCount(0)
  await expect(expand).toBeFocused()
  expect(await editor.innerText()).toBe(draft)

  await expand.click()
  await page.getByRole('dialog', { name: 'Navigation', exact: true }).getByRole('link', { name: 'Prompts', exact: true }).click()
  await guard.getByRole('button', { name: 'Leave and keep draft', exact: true }).click()
  await expect(page).toHaveURL(/\/prompts$/)
  await expect(guard).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Navigation', exact: true })).toHaveCount(0)
  await page.goBack()
  await expect(promptRow).toHaveAttribute('aria-selected', 'true')
  await expect(editor).toBeVisible()
  expect(await editor.innerText()).toBe(draft)
})
