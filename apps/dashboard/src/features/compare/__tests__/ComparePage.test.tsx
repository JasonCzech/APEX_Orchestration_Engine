import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { queryKeys } from '@/api/queryKeys'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { ComparePage } from '../ComparePage'
import {
  COMPARE_DETAIL_A,
  compareDetailHandler,
  compareListHandler,
  RUN_A_ID,
  RUN_B_ID,
  RUN_C_ID,
} from './compareFixtures'

function LocationProbe({ label }: { label: string }) {
  const location = useLocation()
  return <div data-testid={label}>{location.pathname + location.search}</div>
}

/** Mounts the page on a memory router with probes for its navigation targets. */
function renderCompare(
  initialEntry: string,
  queryClient = createTestQueryClient(),
) {
  const router = createMemoryRouter(
    [
      { path: '/runs/compare', element: <ComparePage /> },
      { path: '/runs', element: <LocationProbe label="runs-list" /> },
      { path: '/runs/:threadId', element: <LocationProbe label="run-detail" /> },
    ],
    { initialEntries: [initialEntry] },
  )
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

/** Decoded ?ids= from the router's current location (commas arrive %2C-encoded). */
function idsParam(router: ReturnType<typeof createMemoryRouter>): string | null {
  return new URLSearchParams(router.state.location.search).get('ids')
}

describe('ComparePage', () => {
  it('shows the selection empty state when fewer than 2 ids are given', async () => {
    renderCompare(`/runs/compare?ids=${RUN_A_ID}`)

    expect(await screen.findByRole('heading', { level: 2, name: 'Compare Runs' })).toBeInTheDocument()
    expect(screen.getByText(/One run selected — comparison needs at least two/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to runs' })).toHaveAttribute('href', '/runs')
  })

  it('parses ?ids= leniently: duplicates and whitespace collapse to unique ids', async () => {
    server.use(compareDetailHandler())
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_A_ID},%20${RUN_B_ID}%20,`)

    expect(await screen.findByTestId(`compare-col-${RUN_A_ID}`)).toBeInTheDocument()
    expect(screen.getByTestId(`compare-col-${RUN_B_ID}`)).toBeInTheDocument()
    expect(screen.getByText('2 runs')).toBeInTheDocument()
    // The duplicate id produced one column, not two.
    expect(screen.getAllByTestId(/^compare-col-/)).toHaveLength(2)
  })

  it('renders side-by-side columns: title link, status badge, engine chip, phase cells', async () => {
    server.use(compareDetailHandler())
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    const colA = within(await screen.findByTestId(`compare-col-${RUN_A_ID}`))
    expect(colA.getByRole('link', { name: /Checkout latency regression/ })).toHaveAttribute(
      'href',
      `/runs/${RUN_A_ID}`,
    )
    expect(colA.getByText('idle')).toHaveClass('status-badge', 'success')
    expect(colA.getByText('sim')).toHaveClass('dash-context-chip')

    const colB = within(screen.getByTestId(`compare-col-${RUN_B_ID}`))
    expect(colB.getByText('error')).toHaveClass('status-badge', 'danger')
    expect(colB.getByText('apexload')).toHaveClass('dash-context-chip')

    // Phase cells carry status + duration + attempt; unplanned phases render an em dash.
    const execA = within(screen.getByTestId(`compare-phase-execution-${RUN_A_ID}`))
    expect(execA.getByText('succeeded')).toHaveClass('status-badge', 'success')
    expect(execA.getByText('5m')).toBeInTheDocument()
    expect(execA.getByText('attempt 1')).toBeInTheDocument()
    expect(screen.getByTestId(`compare-phase-postmortem-${RUN_A_ID}`)).toHaveTextContent('—')
  })

  it('highlights the slowest phase cell amber only when it exceeds 1.5x the fastest', async () => {
    server.use(compareDetailHandler())
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    await screen.findByTestId(`compare-col-${RUN_A_ID}`)
    // story_analysis: 10s vs 4s -> B is slow.
    expect(screen.getByTestId(`compare-phase-story_analysis-${RUN_B_ID}`)).toHaveClass(
      'compare-cell--slow',
    )
    expect(screen.getByTestId(`compare-phase-story_analysis-${RUN_A_ID}`)).not.toHaveClass(
      'compare-cell--slow',
    )
    // test_planning: 70s vs 60s -> within bounds, no highlight either side.
    expect(screen.getByTestId(`compare-phase-test_planning-${RUN_B_ID}`)).not.toHaveClass(
      'compare-cell--slow',
    )
    expect(screen.getByTestId(`compare-phase-test_planning-${RUN_A_ID}`)).not.toHaveClass(
      'compare-cell--slow',
    )
  })

  it('tints engine KPI cells best/worst per row and shows passed badges', async () => {
    server.use(compareDetailHandler())
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    await screen.findByText('Engine KPIs')
    // tps_avg: higher is better -> A best, B worst.
    expect(screen.getByTestId(`compare-kpi-tps_avg-${RUN_A_ID}`)).toHaveClass('compare-cell--best')
    expect(screen.getByTestId(`compare-kpi-tps_avg-${RUN_B_ID}`)).toHaveClass('compare-cell--worst')
    // p95_ms: lower is better -> A (200) best, B (350) worst.
    expect(screen.getByTestId(`compare-kpi-p95_ms-${RUN_A_ID}`)).toHaveClass('compare-cell--best')
    expect(screen.getByTestId(`compare-kpi-p95_ms-${RUN_B_ID}`)).toHaveClass('compare-cell--worst')
    expect(screen.getByTestId(`compare-kpi-p95_ms-${RUN_A_ID}`)).toHaveTextContent('200 ms')
    expect(screen.getByTestId(`compare-kpi-error_rate-${RUN_B_ID}`)).toHaveTextContent('2.00%')
    // Passed badges per run.
    expect(within(screen.getByTestId(`compare-passed-${RUN_A_ID}`)).getByText('passed')).toHaveClass(
      'status-badge',
      'success',
    )
    expect(within(screen.getByTestId(`compare-passed-${RUN_B_ID}`)).getByText('failed')).toHaveClass(
      'status-badge',
      'danger',
    )
  })

  it('renders the artifacts/warnings counts mini-table per phase', async () => {
    server.use(compareDetailHandler())
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    await screen.findByText('Artifacts · Warnings')
    expect(screen.getByTestId(`compare-counts-test_planning-${RUN_A_ID}`)).toHaveTextContent('2 · 1')
    expect(screen.getByTestId(`compare-counts-test_planning-${RUN_B_ID}`)).toHaveTextContent('0 · 0')
    expect(screen.getByTestId(`compare-counts-execution-${RUN_B_ID}`)).toHaveTextContent('1 · 1')
  })

  it('removes a run via the column X, updating ?ids= (and falling to the empty state at 1)', async () => {
    server.use(compareDetailHandler())
    const user = userEvent.setup()
    const router = renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    await screen.findByTestId(`compare-col-${RUN_B_ID}`)
    await user.click(screen.getByRole('button', { name: 'Remove Nightly soak from comparison' }))

    await waitFor(() => expect(idsParam(router)).toBe(RUN_A_ID))
    expect(await screen.findByText(/One run selected — comparison needs at least two/)).toBeInTheDocument()
  })

  it('adds a run through the picker (recent runs minus the already-selected ids)', async () => {
    server.use(compareDetailHandler(), compareListHandler())
    const user = userEvent.setup()
    const router = renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    await screen.findByTestId(`compare-col-${RUN_A_ID}`)
    await user.click(screen.getByRole('button', { name: 'Add run' }))

    const panel = within(await screen.findByLabelText('Add a run to compare'))
    expect(await panel.findByText('Throughput probe')).toBeInTheDocument()
    // Selected runs are excluded from the picker.
    expect(panel.queryByText('Checkout latency regression')).not.toBeInTheDocument()
    expect(panel.queryByText('Nightly soak')).not.toBeInTheDocument()

    await user.click(panel.getByRole('button', { name: /Throughput probe/ }))
    await waitFor(() =>
      expect(idsParam(router)).toBe(`${RUN_A_ID},${RUN_B_ID},${RUN_C_ID}`),
    )
    expect(await screen.findByTestId(`compare-col-${RUN_C_ID}`)).toBeInTheDocument()
    expect(screen.getByText('3 runs')).toBeInTheDocument()
  })

  it('keeps cached recent runs available when the picker refresh fails', async () => {
    server.use(compareDetailHandler(), compareListHandler())
    const queryClient = createTestQueryClient()
    const user = userEvent.setup()
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`, queryClient)

    await screen.findByTestId(`compare-col-${RUN_A_ID}`)
    await user.click(screen.getByRole('button', { name: 'Add run' }))
    const panel = within(await screen.findByLabelText('Add a run to compare'))
    expect(await panel.findByText('Throughput probe')).toBeInTheDocument()

    server.use(
      http.get('*/v1/pipelines', () =>
        HttpResponse.json({ detail: 'recent runs temporarily unavailable' }, { status: 503 }),
      ),
    )
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.lists() })
    })

    expect(await panel.findByText(/Showing cached data/)).toHaveTextContent(
      'recent runs temporarily unavailable',
    )
    expect(panel.getByText('Throughput probe')).toBeInTheDocument()
  })

  it('marks a failed snapshot column without blanking loaded ones', async () => {
    server.use(compareDetailHandler([COMPARE_DETAIL_A])) // B will 404
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)

    const colB = within(await screen.findByTestId(`compare-col-${RUN_B_ID}`))
    expect(colB.getByText('load failed')).toHaveClass('status-badge', 'danger')
    const colA = within(screen.getByTestId(`compare-col-${RUN_A_ID}`))
    expect(colA.getByText('idle')).toBeInTheDocument()
    expect(screen.getByTestId(`compare-phase-execution-${RUN_B_ID}`)).toHaveTextContent('—')
  })
})
