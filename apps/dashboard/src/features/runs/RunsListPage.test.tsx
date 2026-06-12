import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { RunsListPage } from './RunsListPage'
import {
  makeSummaries,
  PIPELINES_FIXTURE,
  pipelinesHandler,
  pipelinesNeverResolves,
  RUN_BUSY,
  RUN_GATED,
} from './runsTestHandlers'

function LocationProbe({ label }: { label: string }) {
  const location = useLocation()
  return <div data-testid={label}>{location.pathname + location.search}</div>
}

/** Mounts the page on a memory router with probe routes for its navigation targets. */
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
  it('renders runs with title, id, status badge, engine chip and em dash for missing engine', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(busyRow.getByText('Checkout latency regression')).toBeInTheDocument()
    expect(busyRow.getByText(RUN_BUSY.thread_id)).toBeInTheDocument()
    expect(busyRow.getByText('busy')).toHaveClass('status-badge', 'accent')
    expect(busyRow.getByText('apexload')).toHaveClass('dash-context-chip')

    const gatedRow = within(screen.getByTestId(`runs-row-${RUN_GATED.thread_id}`))
    expect(gatedRow.getByText('interrupted')).toHaveClass('status-badge', 'warning')
    expect(gatedRow.getByText('—')).toBeInTheDocument() // engine null
  })

  it('renders a warning gate chip linking to the run for pending gates', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    renderRunsPage()

    const chip = await screen.findByRole('link', { name: 'gate: prompt_review' })
    expect(chip).toHaveClass('topbar-meta-chip', 'warning')
    expect(chip).toHaveAttribute('href', `/runs/${RUN_GATED.thread_id}`)
    // no gate chip on the ungated row
    const busyRow = within(screen.getByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(busyRow.queryByRole('link', { name: /gate:/ })).not.toBeInTheDocument()
  })

  it('renders a 7-segment phase strip per row and navigates to the phase on segment click', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    renderRunsPage()

    const busyRow = within(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    const strip = busyRow.getByRole('group', { name: 'Phase progress' })
    const segments = within(strip).getAllByRole('button')
    expect(segments).toHaveLength(7)
    expect(busyRow.getByLabelText('execution — running (attempt 1)')).toHaveClass(
      'phase-seg--running',
    )

    await user.click(busyRow.getByLabelText('execution — running (attempt 1)'))
    expect(await screen.findByTestId('phase-detail')).toHaveTextContent(
      `/runs/${RUN_BUSY.thread_id}/phases/execution`,
    )
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
    // Both the toolbar and the empty state offer clear-filters; use the empty-state CTA.
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
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler) // 2 items < limit 25
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
    expect(next).toBeEnabled() // full page -> assume more
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
    expect(captured.last()?.get('offset')).toBeNull() // offset reset to default
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

  it('navigates to the run detail when a row is clicked', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    renderRunsPage()

    await user.click(await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(await screen.findByTestId('run-detail')).toHaveTextContent(
      `/runs/${RUN_BUSY.thread_id}`,
    )
  })
})
