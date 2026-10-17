import { expect, test } from '@playwright/test'

test('analytics keeps missing measurements distinct from zero and preserves chart gaps', async ({ page }) => {
  let measured = false
  const metrics = { events: 3, errors: 0, input_tokens: 100, output_tokens: 50, total_tokens: 150, cache_read_tokens: 0, cache_creation_tokens: 0, reasoning_tokens: 0, runs: 1 }
  await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'analytics-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', async route => {
    const path = new URL(route.request().url()).pathname
    let json: unknown = {}
    if (path === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Analytics reviewer', role: 'admin', scopes: [] } }
    else if (path === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Analytics reviewer', consumer_type: 'dashboard', role: 'admin', scopes: [], is_unscoped: true, capabilities: {} }
    else if (path === '/v1/pipelines') json = { items: [], limit: 20, offset: 0 }
    else if (path === '/v1/analytics/agents') json = {
      window: { from: '2026-10-15T00:00:00Z', to: '2026-10-18T00:00:00Z', bucket: 'day', group_by: 'model' },
      cost_visible: true,
      totals: { ...metrics, agents: 1, models: 1, latency_samples: measured ? 2 : 0, avg_latency_ms: measured ? 500 : null, p95_latency_ms: measured ? 1000 : null, cost_usd: measured ? 0.1 : null },
      breakdown: [{ ...metrics, key: 'recorded-worker', thread_id: null, latency_samples: measured ? 2 : 0, avg_latency_ms: measured ? 500 : null, p95_latency_ms: measured ? 1000 : null, cost_usd: measured ? 0.1 : null }],
      series: [0, 1, 2].map(index => ({ ...metrics, events: 1, key: 'recorded-worker', bucket_start: `2026-10-${15 + index}T00:00:00Z`, latency_samples: measured && index !== 1 ? 1 : 0, avg_latency_ms: measured && index !== 1 ? index * 500 : null, p95_latency_ms: measured && index !== 1 ? index * 500 : null, cost_usd: measured && index !== 1 ? index * 0.05 : null })),
      page: { limit: 20, offset: 0, total: 1 },
    }
    return route.fulfill({ json })
  })
  await page.goto('/analytics?measure=latency')
  await expect(page.getByRole('heading', { name: 'No recorded average latency measurements', exact: true })).toBeVisible()
  await expect(page.getByTestId('analytics-agent-series-chart')).toHaveCount(0)
  await expect(page.getByTestId('analytics-cost-trend-chart').getByRole('heading', { name: 'No recorded cost measurements' })).toBeVisible()
  measured = true
  await page.reload()
  const chart = page.getByTestId('analytics-agent-series-chart')
  await expect(chart.locator('.recharts-area-dot')).toHaveCount(2)
  await expect(chart.locator('.recharts-xAxis')).toContainText('Oct 16')
  expect((await chart.locator('.recharts-area-curve').getAttribute('d'))?.match(/M/g)).toHaveLength(2)
  expect((await page.getByTestId('analytics-cost-trend-chart').locator('.recharts-area-curve').getAttribute('d'))?.match(/M/g)).toHaveLength(2)
})

test('chart winners stay independent of table sorting across a bounded population', async ({ page }) => {
  const metrics = { events: 1, latency_samples: 1, errors: 0, input_tokens: 100, output_tokens: 50, cache_read_tokens: 0, cache_creation_tokens: 0, reasoning_tokens: 0, runs: 1, cost_usd: 0 }
  const rows = Array.from({ length: 13 }, (_, index) => ({ ...metrics,
    key: index === 12 ? 'winner-13' : `group-${String(index).padStart(2, '0')}`, bucket_start: '2026-10-15T00:00:00Z',
    total_tokens: index === 12 ? 999 : 100, avg_latency_ms: index === 12 ? 999 : 100, p95_latency_ms: index === 12 ? 999 : 5000,
  }))
  await page.addInitScript(() => { localStorage.setItem('apex.apiKey', 'analytics-fixture'); localStorage.setItem('apex.preferences.v1', '{"reduceMotion":true}') })
  await page.route('**/config.json', route => route.fulfill({ json: { dataMode: 'connected', apexOrigin: '', langgraphOrigin: '' } }))
  await page.route('**/v1/**', async route => {
    const url = new URL(route.request().url())
    let json: unknown = {}
    if (url.pathname === '/v1/system/info') json = { name: 'APEX Orchestration', version: 'test', environment: 'test', features: {}, limits: { max_context_packets: 32 }, consumer: { id: 'reviewer', name: 'Analytics reviewer', role: 'admin', scopes: [] } }
    else if (url.pathname === '/v1/auth/me') json = { principal_kind: 'api_consumer', principal_id: 'reviewer', name: 'Analytics reviewer', consumer_type: 'dashboard', role: 'admin', scopes: [], is_unscoped: true, capabilities: {} }
    else if (url.pathname === '/v1/pipelines') json = { items: [], limit: 20, offset: 0 }
    else if (url.pathname === '/v1/analytics/agents') {
      const sort = url.searchParams.get('sort')
      const sorted = [...rows].sort((a, b) => sort === 'key' ? a.key.localeCompare(b.key)
        : (b[sort as 'total_tokens' | 'avg_latency_ms' | 'p95_latency_ms'] ?? 0) - (a[sort as 'total_tokens' | 'avg_latency_ms' | 'p95_latency_ms'] ?? 0))
      json = {
        window: { from: '2026-10-15T00:00:00Z', to: '2026-10-18T00:00:00Z', bucket: 'day', group_by: 'model' }, cost_visible: true,
        totals: { ...metrics, events: 13, agents: 1, models: 13, total_tokens: 2199, avg_latency_ms: 169, p95_latency_ms: 5000 },
        breakdown: sorted.map(row => ({ ...row, thread_id: null })), series: sorted.slice(0, 12), page: { limit: 20, offset: 0, total: 13 },
      }
    }
    return route.fulfill({ json })
  })
  await page.goto('/analytics?sort=key&dir=asc')
  const chart = page.getByTestId('analytics-agent-series-chart')
  await expect(chart.getByText('winner-13', { exact: true })).toBeVisible()
  await expect(page.getByTestId('analytics-chart-population')).toContainText('Charts cover 12 of 13 groups')
  await page.getByRole('group', { name: 'Measure', exact: true }).getByRole('button', { name: 'Latency' }).click()
  await expect(chart.getByRole('heading')).toContainText('Average latency')
  await page.getByTestId('analytics-breakdown-table').getByRole('button', { name: 'p95', exact: true }).click()
  await expect(page).toHaveURL(/sort=p95_latency_ms/)
  await expect(chart.getByText('winner-13', { exact: true })).toBeVisible()
  await expect(page.getByTestId('analytics-agent-top-chart')).toContainText('winner-13')
})
