import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { queryKeys } from '@/api/queryKeys'
import { authenticatedState, createTestQueryClient, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { ComparePage } from '../ComparePage'
import {
  COMPARE_DETAIL_A,
  COMPARE_DETAIL_B,
  COMPARE_DETAIL_C,
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
  it('uses the route heading without repeating it in the comparison toolbar', async () => {
    server.use(compareDetailHandler())
    renderApp({ initialEntries: [`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`], authState: authenticatedState() })
    await screen.findByTestId(`compare-col-${RUN_A_ID}`)
    expect(screen.getAllByRole('heading', { name: /^Compare runs$/i })).toHaveLength(1)
    expect(screen.getByRole('heading', { name: 'Compare runs', level: 1 })).toBeInTheDocument()
  })

  it('shows the selection empty state when fewer than 2 ids are given', async () => {
    renderCompare(`/runs/compare?ids=${RUN_A_ID}`)

    expect(await screen.findByRole('heading', { level: 2, name: 'Choose runs to compare' })).toBeInTheDocument()
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
    expect(colA.getByText('Orchestration complete')).toHaveClass('status-badge', 'success')
    expect(colA.getByText('sim')).toHaveClass('dash-context-chip')

    const colB = within(screen.getByTestId(`compare-col-${RUN_B_ID}`))
    expect(colB.getByText('Orchestration failed')).toHaveClass('status-badge', 'danger')
    expect(colB.getByText('apexload')).toHaveClass('dash-context-chip')

    // Phase cells carry status + duration + attempt; unplanned phases render an em dash.
    const execA = within(screen.getByTestId(`compare-phase-execution-${RUN_A_ID}`))
    expect(execA.getByText('Complete')).toHaveClass('status-badge', 'success')
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

  it('tints completed execution measurements and preserves a failed acceptance result', async () => {
    const failedSla = structuredClone(COMPARE_DETAIL_B)
    ;(failedSla.values as { phase_results: { execution: { status: string } } }).phase_results.execution.status = 'succeeded'
    server.use(compareDetailHandler([COMPARE_DETAIL_A, failedSla]))
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
    expect(within(screen.getByTestId(`compare-passed-${RUN_A_ID}`)).getByText('Passed')).toHaveClass(
      'status-badge',
      'success',
    )
    expect(within(screen.getByTestId(`compare-passed-${RUN_B_ID}`)).getByText('Failed')).toHaveClass(
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
    expect(colA.getByText('Orchestration complete')).toBeInTheDocument()
    expect(screen.getByTestId(`compare-phase-execution-${RUN_B_ID}`)).toHaveTextContent('—')
  })
})

it('does not rank failed collections or give omitted phases measured zero counts', async () => {
  const uncollected = structuredClone(COMPARE_DETAIL_B)
  const values = uncollected.values as { phases_plan: string[]; phase_results: { execution: { test_summary: { kpis: Record<string, number> } } } }
  values.phases_plan = ['execution']
  values.phase_results.execution.test_summary.kpis = { p95_ms: 0, error_rate: 0 }
  server.use(compareDetailHandler([COMPARE_DETAIL_A, uncollected]))
  renderCompare(`/runs/compare?ids=${RUN_B_ID},${RUN_A_ID}`)
  const cell = await screen.findByTestId(`compare-kpi-p95_ms-${RUN_B_ID}`)
  expect(cell).toHaveTextContent('No collected result')
  expect(cell).not.toHaveClass('compare-cell--best')
  expect(screen.getByRole('combobox', { name: 'Comparison baseline' })).toHaveValue(RUN_A_ID)
  expect(screen.getByRole('option', { name: /no collected result/ })).toBeDisabled()
  expect(screen.getByTestId(`compare-passed-${RUN_B_ID}`)).toHaveTextContent('Not available')
  expect(screen.getByTestId(`compare-counts-test_planning-${RUN_B_ID}`)).toHaveTextContent('—')
})

describe('ComparePage acceptance thresholds', () => {
  type Execution = { status: string; acceptance_thresholds?: Record<string, number>; test_summary: { engine: string; passed?: boolean; sla_breaches?: string[] } }
  const executionOf = (detail: typeof COMPARE_DETAIL_A) =>
    (detail.values as { phase_results: { execution: Execution } }).phase_results.execution

  /** A: simulated, passed. B: APEX Load, collected and failed by the engine. */
  function seed(thresholdsA?: Record<string, number>, thresholdsB?: Record<string, number>) {
    const a = structuredClone(COMPARE_DETAIL_A)
    const b = structuredClone(COMPARE_DETAIL_B)
    if (thresholdsA) executionOf(a).acceptance_thresholds = thresholdsA
    const executionB = executionOf(b)
    executionB.status = 'succeeded'
    executionB.test_summary.engine = 'apex_load'
    if (thresholdsB) executionB.acceptance_thresholds = thresholdsB
    return [a, b]
  }
  const cell = (key: string, id: string) => within(screen.getByTestId(`compare-threshold-${key}-${id}`))

  it('shows each threshold under its KPI and judges only the simulator per row', async () => {
    server.use(compareDetailHandler(seed(
      { p95_ms: 250, error_rate: 0.01, tps_avg: 60 },
      { p95_ms: 250, error_rate: 0.01 },
    )))
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)
    await screen.findByText('Engine KPIs')

    const labels = screen.getAllByRole('rowheader').map((header) => header.firstChild?.textContent)
    expect(labels.slice(labels.indexOf('Result'), labels.indexOf('VUsers peak') + 1)).toEqual([
      'Result', 'TPS avg', 'Minimum throughput', 'p95', 'Maximum P95', 'Error rate', 'Maximum error rate', 'VUsers peak',
    ])
    // Simulator: limit plus Met/Not met in the metric's direction.
    expect(cell('p95_ms', RUN_A_ID).getByText('≤ 250 ms')).toBeInTheDocument()
    expect(cell('p95_ms', RUN_A_ID).getByText('Met')).toHaveClass('status-badge', 'success')
    expect(cell('error_rate', RUN_A_ID).getByText('≤ 1.00%')).toBeInTheDocument()
    expect(cell('tps_avg', RUN_A_ID).getByText('≥ 60 TPS')).toBeInTheDocument()
    expect(cell('tps_avg', RUN_A_ID).getByText('Not met')).toHaveClass('status-badge', 'danger')
    // Real engine: p95 350 ms is over 250 ms, but only APEX Load's verdict is shown.
    expect(cell('p95_ms', RUN_B_ID).getByText('≤ 250 ms')).toBeInTheDocument()
    expect(cell('p95_ms', RUN_B_ID).getByText('Assessed by APEX Load')).toBeInTheDocument()
    expect(cell('p95_ms', RUN_B_ID).queryByText(/Met/)).not.toBeInTheDocument()
    expect(screen.getByTestId(`compare-passed-${RUN_B_ID}`)).toHaveTextContent('Failed')
  })

  it('marks limits that differ between runs and runs launched without one', async () => {
    server.use(compareDetailHandler(seed({ p95_ms: 250, tps_avg: 60 }, { p95_ms: 400 })))
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)
    await screen.findByText('Engine KPIs')

    const label = (name: string) => screen.getByRole('rowheader', { name: new RegExp(`^${name}`) })
    expect(label('Maximum P95')).toHaveTextContent('Limits differ')
    expect(label('Minimum throughput')).toHaveTextContent('Limits differ')
    expect(label('Maximum error rate')).toHaveTextContent('Acceptance limit')
    expect(cell('p95_ms', RUN_B_ID).getByText('≤ 400 ms')).toBeInTheDocument()
    expect(cell('tps_avg', RUN_B_ID).getByText('None set')).toBeInTheDocument()
    expect(cell('error_rate', RUN_A_ID).getByText('None set')).toBeInTheDocument()
  })

  it('collapses to one row when no run was launched with thresholds', async () => {
    server.use(compareDetailHandler(seed()))
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)
    await screen.findByText('Engine KPIs')

    expect(screen.getByRole('rowheader', { name: 'Acceptance thresholds' })).toBeInTheDocument()
    expect(screen.getByTestId(`compare-thresholds-${RUN_A_ID}`)).toHaveTextContent('None set')
    expect(screen.getByTestId(`compare-thresholds-${RUN_B_ID}`)).toHaveTextContent('None set')
    expect(screen.queryByRole('rowheader', { name: /^Maximum P95/ })).not.toBeInTheDocument()
  })

  it('shows no thresholds for a run without a collected result', async () => {
    const [a] = seed({ p95_ms: 250 })
    server.use(compareDetailHandler([a!, COMPARE_DETAIL_C]))
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_C_ID}`)
    await screen.findByText('Engine KPIs')

    expect(screen.getByTestId(`compare-threshold-p95_ms-${RUN_C_ID}`)).toHaveTextContent('No collected result')
    expect(screen.getByRole('rowheader', { name: /^Maximum P95/ })).toHaveTextContent('Acceptance limit')
  })

  it('keeps a simulated failure the limits do not explain from reading as Met', async () => {
    const [a, b] = seed({ p95_ms: 250 }, { p95_ms: 250 })
    executionOf(a!).test_summary.passed = false
    executionOf(a!).test_summary.sla_breaches = ['run failed before completion (injected failure)']
    server.use(compareDetailHandler([a!, b!]))
    renderCompare(`/runs/compare?ids=${RUN_A_ID},${RUN_B_ID}`)
    await screen.findByText('Engine KPIs')

    const result = within(screen.getByTestId(`compare-passed-${RUN_A_ID}`))
    expect(result.getByText('Failed')).toBeInTheDocument()
    expect(result.getByText('Not explained by thresholds')).toHaveAttribute('title', 'run failed before completion (injected failure)')
    expect(cell('p95_ms', RUN_A_ID).getByText('Met')).toBeInTheDocument()
    expect(screen.getByTestId(`compare-passed-${RUN_B_ID}`)).not.toHaveTextContent('Not explained')
  })
})
