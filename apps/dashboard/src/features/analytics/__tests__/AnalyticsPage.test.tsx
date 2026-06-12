import { cloneElement, isValidElement, type ReactNode } from 'react'

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { DAY_MS } from '@/components/controls/timeWindow'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { EMPTY_USAGE_FIXTURE, usageHandler } from './analyticsTestHandlers'

// jsdom has no layout, so ResponsiveContainer would render nothing at 0x0.
// Pin the chart size (recharts' own test convention, as in EngineStrip.test).
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
  it('round-trips URL filters into the request and the controls', async () => {
    const usage = usageHandler()
    server.use(usage.handler)
    renderAnalytics(
      '?from=2026-06-10T00:00:00.000Z&to=2026-06-11T00:00:00.000Z&bucket=hour&project=proj-alpha',
    )

    await screen.findByTestId('analytics-cards')
    const request = usage.captured[0]!
    expect(request.get('from')).toBe('2026-06-10T00:00:00.000Z')
    expect(request.get('to')).toBe('2026-06-11T00:00:00.000Z')
    expect(request.get('bucket')).toBe('hour')
    expect(request.get('project')).toBe('proj-alpha')

    expect(screen.getByRole('combobox', { name: 'Histogram bucket' })).toHaveValue('hour')
    expect(screen.getByRole('searchbox', { name: 'Filter by project' })).toHaveValue('proj-alpha')
  })

  it('window presets write absolute from/to to the URL and auto-bucket hourly for 24h', async () => {
    const usage = usageHandler()
    server.use(usage.handler)
    const user = userEvent.setup()
    const { router } = renderAnalytics()

    await screen.findByTestId('analytics-cards')
    // Pristine screen: server-default 7d window -> auto bucket "day".
    expect(usage.captured[0]!.get('bucket')).toBe('day')
    expect(usage.captured[0]!.get('from')).toBeNull()

    await user.click(screen.getByRole('button', { name: '24h' }))

    const params = new URLSearchParams(router.state.location.search)
    const from = Date.parse(params.get('from') ?? '')
    const to = Date.parse(params.get('to') ?? '')
    expect(to - from).toBe(DAY_MS)

    // The new window re-queries with the documented <=48h -> hour auto rule.
    await waitFor(() => expect(usage.captured.length).toBeGreaterThan(1))
    const last = usage.captured[usage.captured.length - 1]!
    expect(last.get('bucket')).toBe('hour')
    expect(last.get('from')).toBe(params.get('from'))
    expect(last.get('to')).toBe(params.get('to'))
  })

  it('renders stat cards, surface chips, and both charts from the fixture', async () => {
    const usage = usageHandler()
    server.use(usage.handler)
    renderAnalytics()

    await screen.findByTestId('analytics-cards')
    expect(screen.getByTestId('stat-events')).toHaveTextContent('940')
    const errors = screen.getByTestId('stat-errors')
    expect(errors).toHaveTextContent('47')
    expect(errors).toHaveClass('danger')
    expect(screen.getByTestId('stat-error-rate')).toHaveTextContent('5.0%')
    expect(screen.getByTestId('stat-phases')).toHaveTextContent('42 / 3')

    const chips = screen.getByTestId('surface-chips')
    expect(chips).toHaveTextContent('v1: 900')
    expect(chips).toHaveTextContent('graph: 40')

    // Charts mount real recharts SVG surfaces (container queries, not pixels).
    const events = screen.getByTestId('analytics-events-chart')
    expect(events.querySelector('.recharts-surface')).toBeInTheDocument()
    const actions = screen.getByTestId('analytics-actions-chart')
    expect(actions.querySelector('.recharts-surface')).toBeInTheDocument()
    expect(actions.textContent).toContain('pipelines.list')
    // Long action labels truncate (mono axis ticks).
    expect(actions.textContent).toContain('work_tracking.query.exe…')
  })

  it('shows the empty state for a window with no usage', async () => {
    const usage = usageHandler(EMPTY_USAGE_FIXTURE)
    server.use(usage.handler)
    renderAnalytics()

    expect(await screen.findByText('No usage in this window')).toBeInTheDocument()
    // Cards still render the zeroed totals; charts are replaced by the empty state.
    expect(screen.getByTestId('stat-events')).toHaveTextContent('0')
    expect(screen.getByTestId('stat-errors')).not.toHaveClass('danger')
    expect(screen.queryByTestId('analytics-events-chart')).not.toBeInTheDocument()
  })
})
