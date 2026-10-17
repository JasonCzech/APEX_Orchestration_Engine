import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { cloneElement, isValidElement, type ReactNode } from 'react'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { RunsListPage } from './RunsListPage'
import {
  makeSummaries,
  makeStrip,
  PIPELINES_FIXTURE,
  pipelinesHandler,
  pipelinesNeverResolves,
  RUN_BUSY,
  RUN_GATED,
} from './runsTestHandlers'

vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({
      value,
      'aria-label': ariaLabel,
    }: {
      value: string
      'aria-label'?: string
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
        'aria-label': ariaLabel,
        readOnly: true,
        value,
      }),
  }
})

vi.mock('@/streaming/usePipelineStream', () => ({
  useRunLiveness: () => ({
    runId: null,
    stream: {
      status: 'idle',
      phaseProgress: {},
      toolCalls: [],
      engineStats: { samples: [], latest: null },
      pendingGateHint: null,
    },
  }),
}))

vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children as React.ReactElement<{ width?: number; height?: number }>, {
            width: 600,
            height: 180,
          })
        : children,
  }
})

function LocationProbe({ label }: { label: string }) {
  const location = useLocation()
  return <div data-testid={label}>{location.pathname + location.search}</div>
}

function detailHandler(detail: PipelineDetail) {
  return http.get('*/v1/pipelines/:threadId', () => HttpResponse.json(detail))
}

const RUN_BUSY_DETAIL: PipelineDetail = {
  ...RUN_BUSY,
  values: {
    title: RUN_BUSY.title,
    current_phase: 'execution',
    phases_plan: [
      'story_analysis',
      'test_planning',
      'env_triage',
      'script_scenario',
      'execution',
      'reporting',
      'postmortem',
    ],
    phase_results: {
      story_analysis: { phase: 'story_analysis', status: 'succeeded', attempt: 1 },
      test_planning: { phase: 'test_planning', status: 'succeeded', attempt: 1 },
      env_triage: { phase: 'env_triage', status: 'skipped', attempt: 1 },
      script_scenario: { phase: 'script_scenario', status: 'succeeded', attempt: 2 },
      execution: {
        phase: 'execution',
        status: 'running',
        attempt: 1,
        started_at: '2026-06-11T10:05:00Z',
        duration_s: 75,
        artifact_ids: ['exec-metrics'],
        warnings: ['Generator pool is approaching capacity.'],
        summary: 'Execution is still streaming results.',
      },
    },
    artifacts: [],
    dialogue: [],
  },
  interrupts: [],
}

function renderRunsPage(initialEntry = '/runs') {
  const router = createMemoryRouter(
    [
      { path: '/runs', element: <RunsListPage /> },
      { path: '/runs/new', element: <LocationProbe label="new-run" /> },
      { path: '/runs/:threadId', element: <LocationProbe label="run-detail" /> },
      { path: '/runs/:threadId/reviews/:interruptId', element: <LocationProbe label="run-review" /> },
      { path: '/runs/:threadId/phases/:phase', element: <LocationProbe label="phase-detail" /> },
    ],
    { initialEntries: [initialEntry] },
  )
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

describe('RunsListPage', () => {
  it('sorts only the loaded page and exposes the active order to assistive technology', async () => {
    server.use(pipelinesHandler([RUN_GATED, RUN_BUSY]).handler)
    const user = userEvent.setup()
    renderRunsPage()
    await screen.findByTestId(`runs-row-${RUN_GATED.thread_id}`)
    expect(screen.getByText('Sorting applies to this page.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Sort this page by Run' }))
    expect(screen.getAllByTestId(/^runs-row-/).map(row => row.getAttribute('data-testid'))).toEqual([`runs-row-${RUN_BUSY.thread_id}`, `runs-row-${RUN_GATED.thread_id}`])
    expect(screen.getByRole('button', { name: 'Sort this page by Run' }).closest('th')).toHaveAttribute('aria-sort', 'ascending')
    await user.click(screen.getByRole('button', { name: 'Sort this page by Run' }))
    expect(screen.getByRole('button', { name: 'Sort this page by Run' }).closest('th')).toHaveAttribute('aria-sort', 'descending')
    expect(screen.getAllByTestId(/^runs-row-/)[0]).toHaveAttribute('data-testid', `runs-row-${RUN_GATED.thread_id}`)
  })

  it('distinguishes Open run actions when run titles are duplicated', async () => {
    const duplicate = { ...RUN_BUSY, thread_id: 'duplicate-run-id' }
    server.use(pipelinesHandler([RUN_BUSY, duplicate]).handler)
    renderRunsPage()
    expect(await screen.findByRole('button', { name: `Open run ${RUN_BUSY.title} (${RUN_BUSY.thread_id})` })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: `Open run ${RUN_BUSY.title} (${duplicate.thread_id})` })).toBeInTheDocument()
    expect(screen.getByText(duplicate.thread_id)).toBeVisible()
  })

  beforeEach(() => {
    localStorage.setItem('apex.preferences.v1', JSON.stringify({ rows: 25 }))
    server.use(http.get('*/v1/catalog/applications', () => HttpResponse.json([])))
  })
  it('renders runs with title, id, project, application, phase count, verdict, and status', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(busyRow.getByText('Checkout latency regression')).toBeInTheDocument()
    expect(busyRow.getByText(RUN_BUSY.thread_id)).toBeInTheDocument()
    expect(busyRow.getByText('proj-alpha')).toBeInTheDocument()
    expect(busyRow.getByText('app-storefront')).toBeInTheDocument()
    expect(busyRow.getByText('3/7 phases succeeded · 1 skipped')).toBeInTheDocument()
    expect(busyRow.getByText('30m')).toBeInTheDocument()
    expect(busyRow.getByText('Not available')).toHaveClass('status-badge', 'neutral')
    expect(busyRow.getByText('Running')).toHaveClass('status-badge', 'accent')

    const gatedRow = within(screen.getByTestId(`runs-row-${RUN_GATED.thread_id}`))
    expect(gatedRow.getByText('Not available')).toHaveClass('status-badge', 'neutral')
    expect(gatedRow.getByText('Awaiting review')).toHaveClass('status-badge', 'warning')
    expect(gatedRow.getByText('—')).toBeInTheDocument()
  })

  it('does not label idle failed, aborted, or inconsistent runs as GO', async () => {
    const failed = {
      ...makeSummaries(1)[0]!,
      thread_id: 'run-failed',
      title: 'Failed run',
      phase_strip: makeStrip({ execution: { status: 'failed', attempt: 1 } }),
    }
    const aborted = {
      ...makeSummaries(1)[0]!,
      thread_id: 'run-aborted',
      title: 'Aborted run',
      phase_strip: makeStrip({ execution: { status: 'aborted', attempt: 1 } }),
    }
    const inconsistent = {
      ...makeSummaries(1)[0]!,
      thread_id: 'run-inconsistent',
      title: 'Stopped while running',
      phase_strip: makeStrip({ execution: { status: 'running', attempt: 1 } }),
    }
    server.use(pipelinesHandler([failed, aborted, inconsistent]).handler)
    renderRunsPage()

    for (const id of ['run-failed', 'run-aborted', 'run-inconsistent']) {
      expect(within(await screen.findByTestId(`runs-row-${id}`)).getByText('Not available')).toHaveClass(
        'neutral',
      )
    }
  })

  it('renders a warning gate action that opens the consolidated workspace', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    const router = renderRunsPage()

    const chip = await screen.findByRole('button', { name: 'Review prompts for Nightly soak, Test Planning' })
    expect(chip).toHaveClass('topbar-meta-chip', 'warning')
    const busyRow = within(screen.getByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(busyRow.queryByRole('button', { name: /review prompts/i })).not.toBeInTheDocument()

    await user.click(chip)
    expect(router.state.location.pathname).toBe('/runs/run-gated-2/reviews/int-7')
    expect(router.state.location.search).toContain('source=history')
  })

  it('opens the shared Run Workspace with an operator summary and technical JSON', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler, detailHandler(RUN_BUSY_DETAIL))
    const user = userEvent.setup()
    renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    await user.click(busyRow.getByRole('button', { name: `Open run Checkout latency regression (${RUN_BUSY.thread_id})` }))

    const workspace = await screen.findByRole('region', { name: 'Run workspace' })
    expect(within(workspace).getByRole('heading', { name: 'Checkout latency regression' })).toHaveFocus()
    expect(within(workspace).getByText('Test result:')).toBeInTheDocument()
    expect(within(workspace).getByLabelText('Phase snapshot')).toHaveTextContent('1 phase artifact')
    expect(within(workspace).getByText('Execution is still streaming results.')).toBeInTheDocument()
    expect(within(workspace).getByText('Generator pool is approaching capacity.')).toBeInTheDocument()
    expect(within(workspace).getByRole('button', { name: /execution — executing/i })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(workspace).getByRole('tab', { name: 'Phase details' }))
    await user.click(within(workspace).getByText('Technical phase output'))
    expect(within(workspace).getByLabelText('Phase output JSON')).toHaveTextContent(
      '"status": "running"',
    )
  })

  it('keeps Timeline inside the selected history workspace and restores phase context', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler, detailHandler(RUN_BUSY_DETAIL))
    const user = userEvent.setup()
    const router = renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    await user.click(
      busyRow.getByRole('button', {
        name: `Open run Checkout latency regression (${RUN_BUSY.thread_id})`,
      }),
    )

    const workspace = await screen.findByRole('region', { name: 'Run workspace' })
    await user.click(within(workspace).getByRole('button', { name: 'Timeline' }))

    expect(within(workspace).getByRole('region', { name: 'Run timeline' })).toHaveTextContent(
      'Execution started',
    )
    expect(router.state.location.pathname).toBe('/runs')
    expect(router.state.location.search).toContain('runSection=timeline')
    expect(screen.getByRole('complementary', { name: 'Run history' })).toBeInTheDocument()

    await user.click(within(workspace).getByRole('button', { name: 'Workspace' }))
    expect(router.state.location.search).toContain('runSection=workspace')
    expect(router.state.location.search).toContain('phase=execution')
    expect(within(workspace).getByLabelText('Phase snapshot')).toBeInTheDocument()
  })

  it('shows the empty state with a new-run CTA when there are no runs and no filters', async () => {
    server.use(pipelinesHandler([]).handler)
    renderRunsPage()

    expect(await screen.findByText('No runs found')).toBeInTheDocument()
    const cta = screen.getByRole('link', { name: 'Start a new run' })
    expect(cta).toHaveAttribute('href', '/runs/new')
  })

  it('offers clear-filters in the empty state when filters are active, and clears the URL', async () => {
    server.use(pipelinesHandler([]).handler)
    const user = userEvent.setup()
    const router = renderRunsPage('/runs?q=zzz&status=error')

    expect(await screen.findByText('No runs found')).toBeInTheDocument()
    const buttons = screen.getAllByRole('button', { name: 'Clear filters' })
    expect(buttons).toHaveLength(2)
    await user.click(buttons[1] as HTMLElement)
    await waitFor(() => expect(router.state.location.search).toBe(''))
  })

  it('shows a problem card on error and recovers via retry refetch', async () => {
    const { handler, captured } = pipelinesHandler(PIPELINES_FIXTURE, { failFirst: true })
    server.use(handler)
    const user = userEvent.setup()
    renderRunsPage()

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('projection unavailable')

    await user.click(within(alert).getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText('Checkout latency regression')).toBeInTheDocument()
    expect(captured.queries.length).toBe(2)
  })

  it('shows a pulsing skeleton while the first page loads', async () => {
    server.use(pipelinesNeverResolves())
    renderRunsPage()

    expect(await screen.findByRole('status', { name: 'Loading runs' })).toHaveAttribute(
      'aria-busy',
      'true',
    )
  })

  it('does not present the previous list as matching a newly committed filter', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    renderRunsPage()

    expect(await screen.findByText('Checkout latency regression')).toBeInTheDocument()
    server.use(pipelinesNeverResolves())
    await user.type(screen.getByRole('searchbox', { name: 'Search runs' }), 'new-filter')

    expect(await screen.findByRole('status', { name: 'Loading runs' })).toBeInTheDocument()
    expect(screen.queryByText('Checkout latency regression')).not.toBeInTheDocument()
  })

  it('disables pagination at the bounds (no total: next disabled when items < limit)', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    renderRunsPage()

    expect(await screen.findByText('1–2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
  })

  it('pages forward and back via offset in the URL', async () => {
    server.use(pipelinesHandler(makeSummaries(25)).handler)
    const user = userEvent.setup()
    const router = renderRunsPage('/runs?offset=25')

    expect(await screen.findByText('26–50')).toBeInTheDocument()
    const next = screen.getByRole('button', { name: 'Next' })
    const prev = screen.getByRole('button', { name: 'Previous' })
    expect(next).toBeEnabled()
    expect(prev).toBeEnabled()

    await user.click(next)
    await waitFor(() => expect(router.state.location.search).toContain('offset=50'))

    await user.click(screen.getByRole('button', { name: 'Previous' }))
    await waitFor(() => expect(router.state.location.search).toContain('offset=25'))
  })

  it('keeps Previous available when a speculative next page is empty', async () => {
    server.use(
      http.get('*/v1/pipelines', ({ request }) => {
        const url = new URL(request.url)
        const offset = Number(url.searchParams.get('offset') ?? '0')
        return HttpResponse.json({
          items: offset === 0 ? makeSummaries(25) : [],
          limit: 25,
          offset,
        })
      }),
    )
    const user = userEvent.setup()
    const router = renderRunsPage()

    expect(await screen.findByText('1–25')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Next' }))

    expect(await screen.findByRole('heading', { name: 'No more runs' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    const previous = screen.getByRole('button', { name: 'Previous' })
    expect(previous).toBeEnabled()

    await user.click(previous)
    await waitFor(() => expect(router.state.location.search).toBe('?limit=25'))
    expect(await screen.findByText('1–25')).toBeInTheDocument()
  })

  it('stops at the backend result-window boundary and explains the limit', async () => {
    server.use(pipelinesHandler(makeSummaries(25)).handler)
    renderRunsPage('/runs?offset=10000')

    expect(await screen.findByText('10001–10025')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByText(/Reached the runs result-window limit/)).toBeInTheDocument()
  })

  it('resets to the first page when the shared Rows control changes', async () => {
    const { handler, captured } = pipelinesHandler(PIPELINES_FIXTURE)
    server.use(handler)
    const user = userEvent.setup()
    renderRunsPage('/runs?offset=25')
    await screen.findByRole('combobox', { name: 'Runs rows per page' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Runs rows per page' }), '10')
    await waitFor(() => expect(captured.last()?.get('limit')).toBe('10'))
    expect(captured.last()?.get('offset')).toBeNull()
  })

  it('debounces the search input into ?q= and resets the offset', async () => {
    const { handler, captured } = pipelinesHandler(PIPELINES_FIXTURE)
    server.use(handler)
    const user = userEvent.setup()
    const router = renderRunsPage('/runs?offset=25')

    await screen.findByText('Checkout latency regression')
    await user.type(screen.getByRole('searchbox', { name: 'Search runs' }), 'soak')

    await waitFor(() => expect(router.state.location.search).toContain('q=soak'), { timeout: 2000 })
    await waitFor(() => expect(captured.last()?.get('q')).toBe('soak'))
    expect(captured.last()?.get('offset')).toBeNull()
  })

  it('applies the status select to the URL and the request', async () => {
    const { handler, captured } = pipelinesHandler(PIPELINES_FIXTURE)
    server.use(handler)
    const user = userEvent.setup()
    const router = renderRunsPage()

    await screen.findByText('Checkout latency regression')
    await user.selectOptions(
      screen.getByRole('combobox', { name: 'Filter by status' }),
      'interrupted',
    )

    await waitFor(() => expect(router.state.location.search).toContain('status=interrupted'))
    await waitFor(() => expect(captured.last()?.get('status')).toBe('interrupted'))
  })

  it('opens the same Run Workspace from the run title', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler, detailHandler(RUN_BUSY_DETAIL))
    const user = userEvent.setup()
    const router = renderRunsPage()

    const row = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    await user.click(row.getByRole('button', { name: 'Open Checkout latency regression details' }))
    expect(await screen.findByRole('region', { name: 'Run workspace' })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/runs')
    expect(router.state.location.search).toContain(`selected=${RUN_BUSY.thread_id}`)
  })
})
