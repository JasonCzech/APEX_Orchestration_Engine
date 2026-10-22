import { expect, test, type Page } from '@playwright/test'

test.setTimeout(120_000)
test.use({ actionTimeout: 10_000 })

async function resetDemo(page: Page) {
  const account = page.getByRole('button', { name: 'Account options for Demo Presenter' })
  await account.click()
  const navigation = page.getByRole('dialog', { name: 'Navigation', exact: true })
  await expect(navigation.getByText('Demo Presenter', { exact: true })).toBeVisible()
  await expect(navigation.getByText('Offline simulation', { exact: true })).toBeVisible()
  await navigation.getByRole('button', { name: 'Reset demo', exact: true }).click()
  await navigation.getByRole('button', { name: 'Collapse sidebar', exact: true }).click()
  await expect(account).toBeFocused()
}

test('production demo completes the offline Jira Align to Jira Cloud journey', async ({ page }) => {
  const applicationDataRequests: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.resourceType() !== 'document' && /^\/(v1|threads|runs|assistants)(\/|$)/.test(path)) {
      applicationDataRequests.push(path)
    }
  })

  await page.goto('/')
  await expect(page.getByRole('button', { name: 'Account options for Demo Presenter' })).toBeVisible()
  await expect(page.getByText('DEMO · OFFLINE')).toBeVisible()
  await resetDemo(page)

  await page.goto('/runs/new?draft=draft-esim')
  await expect(page.getByLabel('Title', { exact: true })).toHaveValue('ACC Add-a-Line Peak Certification')
  // The traffic entry's environment is the execution target.
  await expect(page.locator('#wizard-environment')).toHaveValue('env-acc-perf-acc-perfhyper2')
  await page.getByRole('tab', { name: 'Config', exact: true }).click()
  await expect(page.getByText(/pins engine “apexload”/)).toBeVisible()
  await page.getByRole('radio', { name: /APEX Load/ }).click()
  await page.getByRole('tab', { name: 'Tickets' }).click()
  await page.getByLabel('Find by description').fill('Find Jira Align ACC add-a-line release scope')
  await page.getByRole('button', { name: 'Preview ticket query' }).click()
  await expect(page.getByText('jira_align', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Provider query')).toHaveValue(
    "programId eq 42 and itemtype in ('feature','story') and state ne 'Accepted' and contains(title,'ACC add-a-line')",
  )
  await page.getByRole('button', { name: 'Run query' }).click()
  await expect(page.getByRole('checkbox', { name: 'Select feature:583' })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Select story:9012' })).toBeChecked()
  await page.getByRole('tab', { name: 'Review', exact: true }).click()
  await page.getByRole('button', { name: 'Launch test' }).click()

  await expect(page).toHaveURL(/\/runs\/([^/?]+)$/)
  const threadId = /\/runs\/([^/]+)/.exec(page.url())?.[1]
  expect(threadId).toBeTruthy()
  await page.getByRole('link', { name: 'Review', exact: true }).click()
  await expect(page.getByRole('button', { name: /execution — pending/i })).toBeVisible()

  let gate = page.getByRole('region', { name: 'Review prompts' })
  await gate.getByRole('tab', { name: 'Phase prompt' }).click()
  await gate.getByRole('textbox').fill(
    'Plan eligibility rejection and asynchronous ACC add-a-line fulfillment coverage with retry idempotency and IBM MQ recovery.',
  )
  await gate.getByRole('button', { name: 'Save edits and re-review' }).click()
  await gate.getByText('Technical details', { exact: true }).click()
  await expect(gate.getByText(/int-prompt-revision-\d+/)).toBeVisible()
  await gate.getByRole('button', { name: /Execute Phase/i }).click()
  await expect(page.getByText('Decision receipt', { exact: true })).toBeVisible()

  // A run waiting on a decision opens on it, inside Run detail.
  await page.goto(`/runs/${threadId}`)
  await expect(page).toHaveURL(new RegExp(`/runs/${threadId}/phases/env_triage$`))
  await expect(page.getByRole('link', { name: 'Review', exact: true })).toHaveAttribute('aria-current', 'page')
  gate = page.getByRole('region', { name: 'Review phase results' })
  const composer = gate.getByLabel('Discuss with the agent')
  const exactDiscussion = 'Why did the MQ queue grow and what is the release risk?'
  await composer.fill(exactDiscussion)
  await gate.getByRole('button', { name: 'Ask agent' }).click()
  await expect(gate.getByText(exactDiscussion)).toBeVisible()
  await expect(gate.getByText(/only two consumers are active/i)).toBeVisible()
  await expect(page.getByText('schema drift')).toHaveCount(0)

  await gate.getByRole('button', { name: /Request changes/i }).click()
  await gate.getByLabel('Revision instructions').fill('Correct the BSSe test endpoint configuration and restore four MQ consumers, then rescan.')
  await gate.getByRole('button', { name: /Re-run with Changes/i }).click()
  await expect(gate.getByTestId('gate-summary')).toContainText(/PERFHYPER2 rescan is green/i)
  await page.reload()
  gate = page.getByRole('region', { name: 'Review phase results' })
  await expect(gate.getByTestId('gate-summary')).toContainText(/PERFHYPER2 rescan is green/i)
  await gate.getByRole('button', { name: /Accept phase output & continue/i }).click()
  await expect(page.getByText('Decision receipt', { exact: true })).toBeVisible()
  await page.goto(`/runs/${threadId}/phases/execution?tab=log`)
  await expect(page.getByRole('button', { name: /execution — executing/i })).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('region', { name: 'Live engine stats' })).toContainText(/running/i, { timeout: 10_000 })
  await page.waitForTimeout(4_000)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'ACC Add-a-Line Peak Certification' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Live engine stats' })).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('region', { name: 'Review phase results' })).toBeVisible({ timeout: 25_000 })

  await page.reload()
  gate = page.getByRole('region', { name: 'Review phase results' })
  await expect(gate.getByTestId('gate-summary')).toContainText(/BSSe create-order p95 was 3,650ms/i)
  await gate.getByRole('button', { name: /Accept phase output & continue/i }).click()
  await expect(gate).not.toBeVisible()
  await page.goto(`/runs/${threadId}`)
  await expect(page.getByRole('link', { name: 'Review', exact: true })).toHaveAttribute('href', /int-reporting/)
  await page.getByRole('link', { name: 'Review', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/runs/${threadId}/reviews/int-reporting`))
  gate = page.getByRole('region', { name: 'Review phase results' })
  await expect(gate.getByTestId('gate-summary')).toContainText(/PERF-248 will be created/i)
  await gate.getByRole('button', { name: /Accept phase output & continue/i }).click()
  await expect(page.getByText('Decision receipt', { exact: true })).toBeVisible()
  await page.goto(`/runs/${threadId}/phases/postmortem`)
  await expect(page.getByRole('button', { name: /postmortem — complete/i })).toBeVisible({ timeout: 25_000 })
  const committedDemoState = await page.evaluate(() => JSON.parse(localStorage.getItem('apex.demo.state.v1') ?? '{}'))
  expect(committedDemoState.mutations?.workItems?.upsert?.some((item: { key?: string }) => item.key === 'PERF-248')).toBe(true)
  expect(committedDemoState.mutations?.artifacts?.upsert?.some((entry: [string]) => entry[0] === 'reports/PERF-248-jira-payload')).toBe(true)
  await page.goto(`/runs/${threadId}/artifacts/PERF-248-jira-payload`)
  await expect(page.getByText(/"key": "PERF-248"/).first()).toBeVisible()

  await page.goto('/work-items')
  await page.getByLabel('Saved queries').selectOption({ label: 'Open performance defects' })
  await page.getByRole('button', { name: 'Execute' }).click()
  await expect(page.getByRole('link', { name: 'PERF-248', exact: true })).toBeVisible()

  await resetDemo(page)
  await page.goto('/work-items')
  await page.getByLabel('Saved queries').selectOption({ label: 'Open performance defects' })
  await page.getByRole('button', { name: 'Execute' }).click()
  await expect(page.getByRole('link', { name: 'PERF-248', exact: true })).toHaveCount(0)
  await page.goto('/')
  await expect(page.getByText('Recent runs', { exact: true }).locator('..').getByText('8', { exact: true })).toBeVisible()
  expect(applicationDataRequests).toEqual([])
})

test('APEX Load mutations persist across refresh and reset to canonical fixtures', async ({ page }) => {
  await page.goto('/apex-load')
  await resetDemo(page)
  await page.getByRole('tab', { name: 'Scenarios & schedules' }).click()
  await expect(page.getByText('ACC add-a-line peak certification')).toBeVisible()
  await expect(page.getByText('IBM MQ backlog recovery')).toBeVisible()
  await page.getByRole('button', { name: 'Create scenario' }).click()
  await expect(page.getByRole('button', { name: 'New scenario' })).toBeVisible()
  await page.reload()
  await page.getByRole('tab', { name: 'Scenarios & schedules' }).click()
  await expect(page.getByRole('button', { name: 'New scenario' })).toBeVisible()
  await resetDemo(page)
  await page.reload()
  await page.getByRole('tab', { name: 'Scenarios & schedules' }).click()
  await expect(page.getByRole('button', { name: 'New scenario' })).toHaveCount(0)
})

test('engine selection supports one keyboard tab stop and wrapping arrows', async ({ page }) => {
  await page.goto('/runs/new')
  await page.getByRole('tab', { name: 'Config', exact: true }).click()
  const radios = page.getByRole('radiogroup', { name: 'Execution engine' }).getByRole('radio')
  await radios.first().focus()
  await radios.first().press('ArrowRight')
  await expect(radios.nth(1)).toBeFocused()
  await expect(radios.nth(1)).toHaveAttribute('aria-checked', 'true')
  await expect(radios.first()).toHaveAttribute('tabindex', '-1')
  await radios.nth(1).press('Home')
  await radios.first().press('ArrowLeft')
  await expect(radios.last()).toBeFocused()
  await expect(radios.last()).toHaveAttribute('aria-checked', 'true')
})
