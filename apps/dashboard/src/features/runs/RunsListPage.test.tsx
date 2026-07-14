import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

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
  it('renders runs with title, id, project, application, phase count, verdict, and status', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(busyRow.getByText('Checkout latency regression')).toBeInTheDocument()
    expect(busyRow.getByText(RUN_BUSY.thread_id)).toBeInTheDocument()
    expect(busyRow.getByText('proj-alpha')).toBeInTheDocument()
    expect(busyRow.getByText('app-storefront')).toBeInTheDocument()
    expect(busyRow.getByText('4/7')).toBeInTheDocument()
    expect(busyRow.getByText('30m')).toBeInTheDocument()
    expect(busyRow.getByText('—')).toHaveClass('status-badge', 'neutral')
    expect(busyRow.getByText('busy')).toHaveClass('status-badge', 'accent')

    const gatedRow = within(screen.getByTestId(`runs-row-${RUN_GATED.thread_id}`))
    expect(gatedRow.getByText('Conditional')).toHaveClass('status-badge', 'warning')
    expect(gatedRow.getByText('interrupted')).toHaveClass('status-badge', 'warning')
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
      expect(within(await screen.findByTestId(`runs-row-${id}`)).getByText('NO-GO')).toHaveClass(
        'danger',
      )
    }
  })

  it('renders a warning gate chip linking to the run for pending gates', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    renderRunsPage()

    const chip = await screen.findByRole('link', { name: 'gate: prompt_review' })
    expect(chip).toHaveClass('topbar-meta-chip', 'warning')
    expect(chip).toHaveAttribute('href', `/runs/${RUN_GATED.thread_id}`)

    const busyRow = within(screen.getByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(busyRow.queryByRole('link', { name: /gate:/ })).not.toBeInTheDocument()
  })

  it('opens the inline inspector with seven phase buttons and phase output JSON', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler, detailHandler(RUN_BUSY_DETAIL))
    const user = userEvent.setup()
    renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    await user.click(busyRow.getByRole('button', { name: 'Inspect' }))

    expect(await screen.findByRole('heading', { name: 'Checkout latency regression' })).toBeInTheDocument()
    const inspector = screen.getByRole('region', { name: 'Run inspector' })
    const phaseButtons = within(inspector).getAllByRole('button')
    expect(phaseButtons).toHaveLength(7)
    expect(within(inspector).getByRole('button', { name: /execution/i })).toHaveClass(
      'runs-phase-button',
      'active',
    )
    expect(
      (within(inspector).getByTestId('codemirror') as HTMLTextAreaElement).value,
    ).toContain('"status": "running"')
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
    await waitFor(() => expect(router.state.location.search).toBe('?offset=50'))

    await user.click(screen.getByRole('button', { name: 'Previous' }))
    await waitFor(() => expect(router.state.location.search).toBe('?offset=25'))
  })

  it('debounces the search input into ?q= and resets the offset', async () => {
    const { handler, captured } = pipelinesHandler(PIPELINES_FIXTURE)
    server.use(handler)
    const user = userEvent.setup()
    const router = renderRunsPage('/runs?offset=25')

    await screen.findByText('Checkout latency regression')
    await user.type(screen.getByRole('searchbox', { name: 'Search runs' }), 'soak')

    await waitFor(() => expect(router.state.location.search).toBe('?q=soak'), { timeout: 2000 })
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

    await waitFor(() => expect(router.state.location.search).toBe('?status=interrupted'))
    await waitFor(() => expect(captured.last()?.get('status')).toBe('interrupted'))
  })

  it('navigates to the run detail from the run link', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    renderRunsPage()

    const row = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    await user.click(row.getByRole('link', { name: /Checkout latency regression/ }))
    expect(await screen.findByTestId('run-detail')).toHaveTextContent(`/runs/${RUN_BUSY.thread_id}`)
  })
})
