import { cloneElement, isValidElement, type ReactNode } from 'react'

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'

import { DAY_MS } from '@/components/controls/timeWindow'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import {
  AGENT_ANALYTICS_FIXTURE,
  EMPTY_AGENT_ANALYTICS_FIXTURE,
  ZERO_TOKEN_AGENT_ANALYTICS_FIXTURE,
  agentAnalyticsHandler,
} from './analyticsTestHandlers'

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children as React.ReactElement<{ width?: number; height?: number }>, {
            width: 600,
            height: 220,
          })
        : children,
  }
})

function renderAnalytics(search = '') {
  return renderApp({
    initialEntries: [`/analytics${search}`],
    authState: authenticatedState(),
  })
}

describe('AnalyticsPage', () => {
  it.each([
    { measure: 'tokens', tableSort: 'key', order: 'asc', expectedSort: 'total_tokens' },
    { measure: 'latency', tableSort: 'p95_latency_ms', order: 'desc', expectedSort: 'avg_latency_ms' },
  ])('selects the highest $measure groups independently of table sorting', async ({ measure, tableSort, order, expectedSort }) => {
    const rows = Array.from({ length: 13 }, (_, index) => ({
      ...AGENT_ANALYTICS_FIXTURE.series[0]!, key: index === 12 ? 'winner-13' : `group-${String(index).padStart(2, '0')}`,
      total_tokens: index === 12 ? 999 : 100, avg_latency_ms: index === 12 ? 999 : 100,
      p95_latency_ms: index === 12 ? 999 : 5000,
    }))
    const requests: URLSearchParams[] = []
    server.use(http.get('*/v1/analytics/agents', ({ request }) => {
      const params = new URL(request.url).searchParams
      requests.push(params)
      const sort = params.get('sort')
      const sorted = [...rows].sort((a, b) => sort === 'key' ? a.key.localeCompare(b.key)
        : (b[sort as 'total_tokens' | 'avg_latency_ms' | 'p95_latency_ms'] ?? 0) - (a[sort as 'total_tokens' | 'avg_latency_ms' | 'p95_latency_ms'] ?? 0))
      return HttpResponse.json({ ...AGENT_ANALYTICS_FIXTURE, series: sorted.slice(0, 12),
        breakdown: sorted.map(row => ({ ...row, thread_id: null })), page: { offset: 0, limit: 20, total: 13 } })
    }))
    renderAnalytics(`?measure=${measure}&sort=${tableSort}&dir=${order}`)
    const chart = await screen.findByTestId('analytics-agent-series-chart')
    expect(within(chart).getByText('winner-13')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-agent-top-chart')).toHaveTextContent('winner-13')
    expect(screen.getByTestId('analytics-chart-population')).toHaveTextContent('Charts cover 12 of 13 groups')
    expect(screen.getByTestId('analytics-chart-population')).toHaveTextContent('Remaining groups are excluded')
    expect(requests.some(params => params.get('sort') === expectedSort && params.get('order') === 'desc' && params.get('from') === AGENT_ANALYTICS_FIXTURE.window.from)).toBe(true)
  })

  it('keeps the table available when its separate chart query fails', async () => {
    server.use(http.get('*/v1/analytics/agents', ({ request }) =>
      new URL(request.url).searchParams.get('sort') === 'total_tokens'
        ? HttpResponse.json({ detail: 'Chart query failed' }, { status: 503 })
        : HttpResponse.json(AGENT_ANALYTICS_FIXTURE)))
    renderAnalytics('?sort=key')
    expect(await screen.findByRole('heading', { name: 'Analytics charts unavailable' })).toBeInTheDocument()
    expect(screen.getByTestId('analytics-breakdown-table')).toHaveTextContent('claude-3-5-sonnet-latest')
    expect(screen.queryByTestId('analytics-agent-top-chart')).not.toBeInTheDocument()
  })

  it('keeps legacy recorded averages visible without inventing aggregate weights', async () => {
    const fixture = {
      ...AGENT_ANALYTICS_FIXTURE,
      series: AGENT_ANALYTICS_FIXTURE.series.map(row => ({ ...row, latency_samples: null })),
    }
    server.use(agentAnalyticsHandler(fixture).handler)
    renderAnalytics('?measure=latency')
    expect(await screen.findByRole('heading', { name: 'Latency aggregation unavailable' })).toBeInTheDocument()
    expect(screen.getByTestId('analytics-breakdown-table')).toHaveTextContent('1.4s')
    expect(screen.queryByTestId('analytics-agent-top-chart')).not.toBeInTheDocument()
  })

  it.each(['latency', 'cost'])('does not turn missing %s measurements into zero-valued charts', async measure => {
    const missing = { avg_latency_ms: null, p95_latency_ms: null, cost_usd: null }
    const fixture = {
      ...AGENT_ANALYTICS_FIXTURE,
      totals: { ...AGENT_ANALYTICS_FIXTURE.totals, ...missing },
      series: AGENT_ANALYTICS_FIXTURE.series.map(row => ({ ...row, ...missing })),
      breakdown: AGENT_ANALYTICS_FIXTURE.breakdown.map(row => ({ ...row, ...missing })),
    }
    server.use(agentAnalyticsHandler(fixture).handler)
    renderAnalytics(`?measure=${measure}`)
    expect((await screen.findAllByRole('heading', { name: `No recorded ${measure === 'latency' ? 'average latency' : measure} measurements` })).length).toBeGreaterThan(0)
    expect(screen.queryByTestId('analytics-agent-top-chart')).not.toBeInTheDocument()
    expect(screen.queryByTestId('analytics-agent-series-chart')).not.toBeInTheDocument()
    expect(screen.getByTestId('analytics-cost-trend-chart').querySelector('.recharts-surface')).not.toBeInTheDocument()
  })

  it.each(['latency', 'cost'])('retains legitimate zero %s measurements in charts', async measure => {
    const zero = { avg_latency_ms: 0, p95_latency_ms: 0, cost_usd: 0 }
    const fixture = {
      ...AGENT_ANALYTICS_FIXTURE,
      totals: { ...AGENT_ANALYTICS_FIXTURE.totals, ...zero },
      series: AGENT_ANALYTICS_FIXTURE.series.map(row => ({ ...row, ...zero })),
      breakdown: AGENT_ANALYTICS_FIXTURE.breakdown.map(row => ({ ...row, ...zero })),
    }
    server.use(agentAnalyticsHandler(fixture).handler)
    renderAnalytics(`?measure=${measure}`)
    const chart = await screen.findByTestId('analytics-agent-series-chart')
    expect(chart.querySelectorAll('.recharts-area-dot').length).toBe(3)
    expect(screen.getByTestId('analytics-agent-top-chart')).toHaveTextContent('Top 2 model')
    expect(screen.getByTestId('analytics-cost-trend-chart').querySelector('.recharts-area-curve')).toBeInTheDocument()
  })

  it.each(['model', 'date'])('keeps missing buckets as gaps when grouping by %s', async group => {
    const sample = AGENT_ANALYTICS_FIXTURE.series[0]!
    const fixture = {
      ...AGENT_ANALYTICS_FIXTURE,
      series: [
        { ...sample, bucket_start: '2026-06-05T00:00:00Z', avg_latency_ms: 0 },
        { ...sample, bucket_start: '2026-06-06T00:00:00Z', avg_latency_ms: null, p95_latency_ms: null, cost_usd: null },
        { ...sample, bucket_start: '2026-06-07T00:00:00Z', avg_latency_ms: 1000 },
        { ...sample, key: 'unmeasured-agent', avg_latency_ms: null, p95_latency_ms: null, cost_usd: null },
      ],
    }
    server.use(agentAnalyticsHandler(fixture).handler)
    renderAnalytics(`?measure=latency&group=${group}`)
    const chart = await screen.findByTestId('analytics-agent-series-chart')
    expect(chart.querySelectorAll('.recharts-area-dot')).toHaveLength(2)
    expect(chart.querySelector('.recharts-xAxis')).toHaveTextContent('Jun 6')
    expect(chart.querySelector('.recharts-area-curve')?.getAttribute('d')?.match(/M/g)).toHaveLength(2)
    expect(screen.getByTestId('analytics-agent-top-chart')).toHaveTextContent(`Top 1 ${group === 'date' ? 'date' : 'model'}`)
    expect(chart).not.toHaveTextContent('unmeasured-agent')
    expect(screen.getByTestId('analytics-cost-trend-chart').querySelector('.recharts-area-curve')?.getAttribute('d')?.match(/M/g)).toHaveLength(2)
  })

  it('builds date grouping as one Total trend instead of one series per date', async () => {
    const dateData = {
      ...AGENT_ANALYTICS_FIXTURE,
      series: [
        {
          ...AGENT_ANALYTICS_FIXTURE.series[0]!,
          key: '2026-06-05T00:00:00Z',
        },
        {
          ...AGENT_ANALYTICS_FIXTURE.series[2]!,
          key: '2026-06-06T00:00:00Z',
        },
      ],
    }
    const analytics = agentAnalyticsHandler(dateData)
    server.use(analytics.handler)
    renderAnalytics('?group=date')

    const chart = await screen.findByTestId('analytics-agent-series-chart')
    expect(within(chart).getByText('Total')).toBeInTheDocument()
    expect(chart).toHaveTextContent('Tokens over time')
    expect(chart).not.toHaveTextContent('Tokens over time by date')
    const ticks = chart.querySelector('.recharts-xAxis')?.textContent
    expect(ticks).toContain('Jun 5')
    expect(ticks).not.toContain('Jun 4')
    expect(screen.getByText(/buckets in UTC/)).toHaveTextContent('2026')
    expect(screen.getByRole('button', { name: '7d' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('round-trips agent URL filters into the request and controls', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    renderAnalytics(
      '?from=2026-06-10T00:00:00.000Z&to=2026-06-11T00:00:00.000Z&bucket=hour&project=proj-alpha&group=stage&measure=latency&model=gpt-4o-mini&stage=reporting&agent=reporting.worker&test=run-succeeded&status=error&sort=p95_latency_ms&dir=asc&offset=20',
    )

    await screen.findByTestId('analytics-cards')
    const request = analytics.captured[0]!
    expect(request.get('from')).toBe('2026-06-10T00:00:00.000Z')
    expect(request.get('to')).toBe('2026-06-11T00:00:00.000Z')
    expect(request.get('bucket')).toBe('hour')
    expect(request.get('project')).toBe('proj-alpha')
    expect(request.get('group_by')).toBe('stage')
    expect(request.get('model')).toBe('gpt-4o-mini')
    expect(request.get('stage')).toBe('reporting')
    expect(request.get('agent')).toBe('reporting.worker')
    expect(request.get('test')).toBe('run-succeeded')
    expect(request.get('status')).toBe('error')
    expect(request.get('sort')).toBe('p95_latency_ms')
    expect(request.get('order')).toBe('asc')
    expect(request.get('offset')).toBe('20')

    expect(screen.getByRole('searchbox', { name: 'Filter by project' })).toHaveValue('proj-alpha')
    expect(screen.getByRole('searchbox', { name: 'Filter by run' })).toHaveValue('run-succeeded')
    const groupControl = screen.getByRole('group', { name: 'Group by' })
    const measureControl = screen.getByRole('group', { name: 'Measure' })
    expect(within(groupControl).getByRole('button', { name: 'Phase' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(within(measureControl).getByRole('button', { name: 'Latency' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'error' })).toHaveAttribute('aria-pressed', 'true')
    expect(within(screen.getByRole('group', { name: 'Phases filter' })).getByRole('button', { name: 'reporting' })).toHaveAttribute('aria-pressed', 'true')
  })

  it('window presets write absolute from/to to the URL and auto-bucket hourly for 24h', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    const user = userEvent.setup()
    const { router } = renderAnalytics()

    await screen.findByTestId('analytics-cards')
    expect(analytics.captured[0]!.get('bucket')).toBe('day')
    expect(analytics.captured[0]!.get('from')).toBeNull()

    await user.click(screen.getByRole('button', { name: '24h' }))

    const params = new URLSearchParams(router.state.location.search)
    const from = Date.parse(params.get('from') ?? '')
    const to = Date.parse(params.get('to') ?? '')
    expect(to - from).toBe(DAY_MS)

    await waitFor(() => expect(analytics.captured.length).toBeGreaterThan(1))
    const last = analytics.captured[analytics.captured.length - 1]!
    expect(last.get('bucket')).toBe('hour')
    expect(last.get('from')).toBe(params.get('from'))
    expect(last.get('to')).toBe(params.get('to'))
  })

  it('changes breakdown rows and resets a later page in one query', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    const user = userEvent.setup()
    const { router } = renderAnalytics('?offset=20')
    await screen.findByTestId('analytics-breakdown-table')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Analytics rows per page' }), '50')
    await waitFor(() => {
      const params = new URLSearchParams(router.state.location.search)
      expect(params.get('limit')).toBe('50')
      expect(params.get('offset')).toBeNull()
      expect(analytics.captured.some(params => params.get('limit') === '50' && params.get('offset') === null)).toBe(true)
    })
  })

  it('renders agent KPI cards, charts, and sortable breakdown table', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    const user = userEvent.setup()
    renderAnalytics()

    await screen.findByTestId('analytics-cards')
    expect(screen.getByTestId('stat-total-tokens')).toHaveTextContent('16.2K')
    expect(screen.getByTestId('stat-cost')).toHaveTextContent('$0.18')
    expect(screen.getByTestId('stat-latency')).toHaveTextContent('1.2s')
    expect(screen.getByTestId('stat-latency')).toHaveTextContent('P95 2.4s')
    expect(screen.getByTestId('stat-agents-runs')).toHaveTextContent('7 agents · 3 runs')
    expect(screen.getByTestId('stat-error-rate')).toHaveClass('danger')

    expect((await screen.findByTestId('analytics-agent-series-chart')).querySelector('.recharts-surface')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-agent-top-chart').querySelector('.recharts-surface')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-token-split-chart').querySelector('.recharts-surface')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-cost-trend-chart').querySelector('.recharts-surface')).toBeInTheDocument()

    const table = screen.getByTestId('analytics-breakdown-table')
    expect(within(table).getByText('claude-3-5-sonnet-latest')).toBeInTheDocument()
    expect(within(table).getByText('11.2K')).toBeInTheDocument()
    const filters = screen.getByRole('region', { name: 'Agent filters' })
    expect(within(filters).getByRole('button', { name: 'claude-3-5-sonnet-latest' })).toBeInTheDocument()
    expect(within(filters).queryByRole('button', { name: 'claude-sonnet-4-20250514' })).not.toBeInTheDocument()
    const lines = screen.getByTestId('analytics-agent-series-chart').querySelectorAll('.recharts-legend-icon')
    expect(Array.from(lines).some(line => Boolean(line.getAttribute('stroke-dasharray')))).toBe(true)

    await user.click(within(table).getByRole('button', { name: /p95/i }))
    await waitFor(() => {
      const last = analytics.captured[analytics.captured.length - 1]!
      expect(last.get('sort')).toBe('p95_latency_ms')
      expect(last.get('order')).toBe('desc')
    })
  })

  it('keeps time-series keys stable when the breakdown table is on a later page', async () => {
    const pageTwo = {
      ...AGENT_ANALYTICS_FIXTURE,
      breakdown: [
        {
          ...AGENT_ANALYTICS_FIXTURE.breakdown[0]!,
          key: 'page-two-only-model',
        },
      ],
      page: { limit: 20, offset: 20, total: 21 },
    }
    const analytics = agentAnalyticsHandler(pageTwo)
    server.use(analytics.handler)
    renderAnalytics('?offset=20')

    const series = await screen.findByTestId('analytics-agent-series-chart')
    expect(within(series).getByText('claude-3-5-sonnet-latest')).toBeInTheDocument()
    expect(within(series).getByText('gpt-4o-mini')).toBeInTheDocument()
  })

  it('clamps deep-linked pagination at the backend result-window boundary', async () => {
    const capped = {
      ...AGENT_ANALYTICS_FIXTURE,
      page: { limit: 20, offset: 10_000, total: 10_021 },
    }
    const analytics = agentAnalyticsHandler(capped)
    server.use(analytics.handler)
    renderAnalytics('?offset=999999')

    await screen.findByTestId('analytics-breakdown-table')
    expect(analytics.captured[0]?.get('offset')).toBe('10000')
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByText(/Reached the analytics result-window limit/)).toBeInTheDocument()
  })

  it('explains withheld cost when cost_visible is false', async () => {
    const analytics = agentAnalyticsHandler({
      ...AGENT_ANALYTICS_FIXTURE,
      cost_visible: false,
      totals: { ...AGENT_ANALYTICS_FIXTURE.totals, cost_usd: null },
      breakdown: AGENT_ANALYTICS_FIXTURE.breakdown.map((row) => ({ ...row, cost_usd: null })),
      series: AGENT_ANALYTICS_FIXTURE.series.map((row) => ({ ...row, cost_usd: null })),
    })
    server.use(analytics.handler)
    renderAnalytics()

    await screen.findByTestId('analytics-cards')
    expect(screen.getByTestId('stat-cost')).toHaveTextContent('Not available for your role')
    expect(screen.queryByTestId('analytics-cost-trend-chart')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cost' })).toBeDisabled()
  })

  it('shows the empty state for a window with no agent events', async () => {
    const analytics = agentAnalyticsHandler(EMPTY_AGENT_ANALYTICS_FIXTURE)
    server.use(analytics.handler)
    renderAnalytics()

    expect(await screen.findByText('No agent events in this window')).toBeInTheDocument()
    expect(screen.getByTestId('stat-total-tokens')).toHaveTextContent('—')
    expect(screen.queryByTestId('analytics-agent-series-chart')).not.toBeInTheDocument()
  })

  it('falls back to latency when real backend rows have zero tokens', async () => {
    const analytics = agentAnalyticsHandler(ZERO_TOKEN_AGENT_ANALYTICS_FIXTURE)
    server.use(analytics.handler)
    renderAnalytics()

    expect(await screen.findByTestId('analytics-zero-token-hint')).toHaveTextContent(
      'Token capture begins with live LLM agents',
    )
    expect(screen.getByTestId('stat-total-tokens')).toHaveTextContent('—')
    expect(await screen.findByTestId('analytics-agent-series-chart')).toHaveTextContent(
      'Average latency over time by model',
    )
  })

  it('switches group-by and measure through the segmented controls', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    const user = userEvent.setup()
    renderAnalytics()

    await screen.findByTestId('analytics-cards')
    await user.click(
      within(screen.getByRole('group', { name: 'Group by' })).getByRole('button', { name: 'Phase' }),
    )
    await waitFor(() =>
      expect(analytics.captured[analytics.captured.length - 1]!.get('group_by')).toBe('stage'),
    )

    await user.click(
      within(screen.getByRole('group', { name: 'Measure' })).getByRole('button', {
        name: 'Latency',
      }),
    )
    await waitFor(() =>
      expect(analytics.captured[analytics.captured.length - 1]!.get('sort')).toBe('avg_latency_ms'),
    )
    expect(await screen.findByTestId('analytics-agent-series-chart')).toHaveTextContent(
      /Latency over time by phase/i,
    )
  })

  it('drops a deep-linked cost measure/sort when the server hides cost', async () => {
    const analytics = agentAnalyticsHandler({
      ...AGENT_ANALYTICS_FIXTURE,
      cost_visible: false,
      totals: { ...AGENT_ANALYTICS_FIXTURE.totals, cost_usd: null },
      breakdown: AGENT_ANALYTICS_FIXTURE.breakdown.map((row) => ({ ...row, cost_usd: null })),
      series: AGENT_ANALYTICS_FIXTURE.series.map((row) => ({ ...row, cost_usd: null })),
    })
    server.use(analytics.handler)
    const { router } = renderAnalytics('?measure=cost&sort=cost_usd')

    await screen.findByTestId('analytics-cards')
    // The page self-corrects so the table/query never sort by the hidden cost column.
    await waitFor(() =>
      expect(analytics.captured[analytics.captured.length - 1]!.get('sort')).toBe('total_tokens'),
    )
    const params = new URLSearchParams(router.state.location.search)
    expect(params.get('measure')).toBeNull()
    expect(params.get('sort')).toBeNull()
    expect(screen.getByTestId('stat-cost')).toHaveTextContent('Not available for your role')
  })
})
