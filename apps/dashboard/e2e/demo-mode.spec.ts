import { expect, test } from '@playwright/test'

test.setTimeout(120_000)

test('production demo completes the offline Jira Align to Jira Cloud journey', async ({ page }) => {
  const applicationDataRequests: string[] = []
  page.on('request', (request) => {
    const path = new URL(request.url()).pathname
    if (request.resourceType() !== 'document' && /^\/(v1|threads|runs|assistants)(\/|$)/.test(path)) {
      applicationDataRequests.push(path)
    }
  })

  await page.goto('/')
  await expect(page.getByText('Demo Presenter')).toBeVisible()
  await expect(page.getByText('DEMO · OFFLINE')).toBeVisible()
  await expect(page.getByText('Offline simulation')).toBeVisible()
  await page.getByRole('button', { name: 'Reset demo' }).click()

  await page.goto('/runs/new?draft=draft-esim')
  await expect(page.getByLabel('Title')).toHaveValue('ACC Add-a-Line Peak Certification')
  await expect(page.getByLabel('Environment')).toHaveValue('env-perf')
  await page.getByRole('tab', { name: 'Work Items' }).click()
  await page.getByLabel('Find by description').fill('Find Jira Align ACC add-a-line release scope')
  await page.getByRole('button', { name: 'Translate' }).click()
  await expect(page.getByText('jira_align', { exact: true })).toBeVisible()
  await expect(page.getByLabel('Provider query')).toHaveValue(
    "programId eq 42 and itemtype in ('feature','story') and state ne 'Accepted' and contains(title,'ACC add-a-line')",
  )
  await page.getByRole('button', { name: 'Run query' }).click()
  await expect(page.getByRole('checkbox', { name: 'Select feature:583' })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Select story:9012' })).toBeChecked()
  await page.getByRole('button', { name: 'Review Test' }).click()
  await page.getByRole('button', { name: 'Launch Test' }).click()

  await expect(page).toHaveURL(/\/runs\/([^/]+)\/phases\/test_planning/)
  const threadId = /\/runs\/([^/]+)/.exec(page.url())?.[1]
  expect(threadId).toBeTruthy()
  await expect(page.getByRole('button', { name: /execution — pending/i })).toBeVisible()

  let gate = page.getByRole('region', { name: 'Review prompts gate' })
  await gate.getByRole('tab', { name: 'Phase Prompt' }).click()
  await gate.getByRole('textbox').fill(
    'Plan eligibility rejection and asynchronous ACC add-a-line fulfillment coverage with retry idempotency and IBM MQ recovery.',
  )
  await gate.getByRole('button', { name: /Save Edit & Re-review/i }).click()
  await expect(gate.getByText(/^int-prompt-revision-\d+$/)).toBeVisible()
  await gate.getByRole('button', { name: /Execute Phase/i }).click()

  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page).toHaveURL(new RegExp(`/runs/${threadId}/phases/env_triage`))
  gate = page.getByRole('region', { name: 'Review phase results gate' })
  const composer = gate.getByLabel('Discuss with the agent')
  const exactDiscussion = 'Why did the MQ queue grow and what is the release risk?'
  await composer.fill(exactDiscussion)
  await gate.getByRole('button', { name: 'Message Agent' }).click()
  await expect(gate.getByText(exactDiscussion)).toBeVisible()
  await expect(gate.getByText(/only two consumers are active/i)).toBeVisible()
  await expect(page.getByText('schema drift')).toHaveCount(0)

  await gate.getByRole('button', { name: /Modify & Re-run/i }).click()
  await gate.getByLabel('Revision instructions').fill('Correct the BSSe test endpoint configuration and restore four MQ consumers, then rescan.')
  await gate.getByRole('button', { name: /Re-run with Changes/i }).click()
  await expect(gate.getByTestId('gate-summary')).toContainText(/PERFHYPER2 rescan is green/i)
  await page.reload()
  gate = page.getByRole('region', { name: 'Review phase results gate' })
  await expect(gate.getByTestId('gate-summary')).toContainText(/PERFHYPER2 rescan is green/i)
  await gate.getByRole('button', { name: /Approve & Continue/i }).click()
  await expect(page.getByRole('button', { name: /execution — running/i })).toBeVisible({ timeout: 10_000 })

  await page.goto(`/runs/${threadId}/phases/execution?tab=log`)
  await expect(page.getByRole('region', { name: 'Live engine stats' })).toContainText(/running/i, { timeout: 10_000 })
  await page.waitForTimeout(4_000)
  await page.reload()
  await expect(page.getByRole('heading', { name: 'ACC Add-a-Line Peak Certification' })).toBeVisible()
  await expect(page.getByRole('region', { name: 'Live engine stats' })).toBeVisible({ timeout: 10_000 })
  await expect(page.getByRole('region', { name: 'Review phase results gate' })).toBeVisible({ timeout: 25_000 })

  await page.reload()
  gate = page.getByRole('region', { name: 'Review phase results gate' })
  await expect(gate.getByTestId('gate-summary')).toContainText(/BSSe create-order p95 was 3,650ms/i)
  await gate.getByRole('button', { name: /Approve & Continue/i }).click()
  await page.getByRole('link', { name: 'Review' }).click()
  await expect(page).toHaveURL(new RegExp(`/runs/${threadId}/phases/reporting`))
  gate = page.getByRole('region', { name: 'Review phase results gate' })
  await expect(gate.getByTestId('gate-summary')).toContainText(/PERF-248 will be created/i)
  await gate.getByRole('button', { name: /Approve & Continue/i }).click()
  await expect(page.getByRole('button', { name: /postmortem — succeeded/i })).toBeVisible({ timeout: 10_000 })
  const committedDemoState = await page.evaluate(() => JSON.parse(localStorage.getItem('apex.demo.state.v1') ?? '{}'))
  expect(committedDemoState.mutations?.workItems?.upsert?.some((item: { key?: string }) => item.key === 'PERF-248')).toBe(true)
  expect(committedDemoState.mutations?.artifacts?.upsert?.some((entry: [string]) => entry[0] === 'reports/PERF-248-jira-payload')).toBe(true)
  await page.goto(`/runs/${threadId}/artifacts/PERF-248-jira-payload`)
  await expect(page.getByText(/"key": "PERF-248"/).first()).toBeVisible()

  await page.goto('/work-items')
  await page.getByLabel('Saved queries').selectOption({ label: 'Open performance defects' })
  await page.getByRole('button', { name: 'Execute' }).click()
  await expect(page.getByRole('link', { name: 'PERF-248', exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Reset demo' }).click()
  await page.goto('/work-items')
  await page.getByLabel('Saved queries').selectOption({ label: 'Open performance defects' })
  await page.getByRole('button', { name: 'Execute' }).click()
  await expect(page.getByRole('link', { name: 'PERF-248', exact: true })).toHaveCount(0)
  await page.goto('/')
  await expect(page.getByText('Runs Loaded').locator('..').getByText('8', { exact: true })).toBeVisible()
  expect(applicationDataRequests).toEqual([])
})

test('APEX Load mutations persist across refresh and reset to canonical fixtures', async ({ page }) => {
  await page.goto('/apex-load')
  await page.getByRole('button', { name: 'Reset demo' }).click()
  await expect(page.getByText('ACC add-a-line peak certification')).toBeVisible()
  await expect(page.getByText('IBM MQ backlog recovery')).toBeVisible()
  await page.getByRole('button', { name: 'Create scenario' }).click()
  await expect(page.getByRole('button', { name: 'New scenario' })).toBeVisible()
  await page.reload()
  await expect(page.getByRole('button', { name: 'New scenario' })).toBeVisible()
  await page.getByRole('button', { name: 'Reset demo' }).click()
  await page.reload()
  await expect(page.getByRole('button', { name: 'New scenario' })).toHaveCount(0)
})
