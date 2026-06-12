/**
 * D8 grid affordance: the [Compare] toolbar toggle on /runs reveals a checkbox
 * column; ticking 2+ rows floats a "Compare (N)" bar that navigates to
 * /runs/compare?ids=… . Lives with the compare feature — the RunsListPage edit
 * itself is additive and its own suite stays untouched.
 */
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { RunsListPage } from '@/features/runs/RunsListPage'
import {
  PIPELINES_FIXTURE,
  pipelinesHandler,
  RUN_BUSY,
  RUN_GATED,
} from '@/features/runs/runsTestHandlers'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

function LocationProbe({ label }: { label: string }) {
  const location = useLocation()
  return <div data-testid={label}>{location.pathname + location.search}</div>
}

function renderRunsPage() {
  const router = createMemoryRouter(
    [
      { path: '/runs', element: <RunsListPage /> },
      { path: '/runs/compare', element: <LocationProbe label="compare-page" /> },
      { path: '/runs/:threadId', element: <LocationProbe label="run-detail" /> },
    ],
    { initialEntries: ['/runs'] },
  )
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

describe('RunsListPage compare selection', () => {
  it('reveals checkboxes via the Compare toggle, floats the bar at 2+, and navigates', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    renderRunsPage()

    await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`)
    // Off by default: no checkbox column, no floating bar.
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Compare selection' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Compare' }))
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(PIPELINES_FIXTURE.length)

    // One selection is not enough for the bar.
    await user.click(
      screen.getByRole('checkbox', { name: 'Select Checkout latency regression for compare' }),
    )
    expect(screen.queryByRole('region', { name: 'Compare selection' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('checkbox', { name: 'Select Nightly soak for compare' }))
    const bar = within(screen.getByRole('region', { name: 'Compare selection' }))
    const cta = bar.getByRole('link', { name: 'Compare (2)' })
    expect(cta).toHaveAttribute(
      'href',
      `/runs/compare?ids=${RUN_BUSY.thread_id},${RUN_GATED.thread_id}`,
    )

    await user.click(cta)
    expect(await screen.findByTestId('compare-page')).toHaveTextContent(
      `/runs/compare?ids=${RUN_BUSY.thread_id},${RUN_GATED.thread_id}`,
    )
  })

  it('keeps row navigation intact while selecting, and clears selection on toggle-off', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    renderRunsPage()

    await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`)
    await user.click(screen.getByRole('button', { name: 'Compare' }))
    await user.click(
      screen.getByRole('checkbox', { name: 'Select Checkout latency regression for compare' }),
    )
    await user.click(screen.getByRole('checkbox', { name: 'Select Nightly soak for compare' }))
    expect(screen.getByRole('region', { name: 'Compare selection' })).toBeInTheDocument()

    // Toggling compare off clears the selection and hides the column + bar.
    await user.click(screen.getByRole('button', { name: 'Compare' }))
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Compare selection' })).not.toBeInTheDocument()

    // Row click still navigates to the run detail (checkbox cell swallowed its clicks).
    await user.click(screen.getByTestId(`runs-row-${RUN_BUSY.thread_id}`))
    expect(await screen.findByTestId('run-detail')).toHaveTextContent(
      `/runs/${RUN_BUSY.thread_id}`,
    )
  })
})
