import { cloneElement, isValidElement, type ReactNode } from 'react'

import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

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
    expect(screen.getByRole('searchbox', { name: 'Filter by test' })).toHaveValue('run-succeeded')
    const groupControl = screen.getByRole('group', { name: 'Group by' })
    const measureControl = screen.getByRole('group', { name: 'Measure' })
    expect(within(groupControl).getByRole('button', { name: 'Stage' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(within(measureControl).getByRole('button', { name: 'Latency' })).toHaveAttribute(
      'aria-pressed',
      'true',
    )
    expect(screen.getByRole('button', { name: 'error' })).toHaveAttribute('aria-pressed', 'true')
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

  it('renders agent KPI cards, charts, and sortable breakdown table', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    const user = userEvent.setup()
    renderAnalytics()

    await screen.findByTestId('analytics-cards')
    expect(screen.getByTestId('stat-total-tokens')).toHaveTextContent('16.2K')
    expect(screen.getByTestId('stat-cost')).toHaveTextContent('$0.18')
    expect(screen.getByTestId('stat-latency')).toHaveTextContent('1.2s')
    expect(screen.getByTestId('stat-latency')).toHaveTextContent('p95 2.4s')
    expect(screen.getByTestId('stat-agents-runs')).toHaveTextContent('7 / 3')
    expect(screen.getByTestId('stat-error-rate')).toHaveClass('danger')

    expect(screen.getByTestId('analytics-agent-series-chart').querySelector('.recharts-surface')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-agent-top-chart').querySelector('.recharts-surface')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-token-split-chart').querySelector('.recharts-surface')).toBeInTheDocument()
    expect(screen.getByTestId('analytics-cost-trend-chart').querySelector('.recharts-surface')).toBeInTheDocument()

    const table = screen.getByTestId('analytics-breakdown-table')
    expect(within(table).getByText('claude-3-5-sonnet-latest')).toBeInTheDocument()
    expect(within(table).getByText('11.2K')).toBeInTheDocument()

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

  it('hides cost UI when cost_visible is false', async () => {
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
    expect(screen.queryByTestId('stat-cost')).not.toBeInTheDocument()
    expect(screen.queryByTestId('analytics-cost-trend-chart')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Cost' })).toBeDisabled()
  })

  it('shows the empty state for a window with no agent events', async () => {
    const analytics = agentAnalyticsHandler(EMPTY_AGENT_ANALYTICS_FIXTURE)
    server.use(analytics.handler)
    renderAnalytics()

    expect(await screen.findByText('No agent events in this window')).toBeInTheDocument()
    expect(screen.getByTestId('stat-total-tokens')).toHaveTextContent('0')
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
    expect(screen.getByTestId('analytics-agent-series-chart')).toHaveTextContent(
      'Latency over time by model',
    )
  })

  it('switches group-by and measure through the segmented controls', async () => {
    const analytics = agentAnalyticsHandler()
    server.use(analytics.handler)
    const user = userEvent.setup()
    renderAnalytics()

    await screen.findByTestId('analytics-cards')
    await user.click(
      within(screen.getByRole('group', { name: 'Group by' })).getByRole('button', { name: 'Stage' }),
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
      expect(analytics.captured[analytics.captured.length - 1]!.get('sort')).toBe('p95_latency_ms'),
    )
    expect(screen.getByTestId('analytics-agent-series-chart')).toHaveTextContent(
      /Latency over time by stage/i,
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
    expect(screen.queryByTestId('stat-cost')).not.toBeInTheDocument()
  })
})
