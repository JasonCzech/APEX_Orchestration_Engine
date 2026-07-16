/**
 * The phase-subset re-run entry points (plan Part 2 §4): runs-grid row
 * overflow menu and run-detail header split button. Both funnel into
 * PreflightModal with the documented preselection.
 */
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { RunsListPage } from '../RunsListPage'
import { PIPELINES_FIXTURE, pipelinesHandler, RUN_BUSY } from '../runsTestHandlers'
import { PIPELINE_DETAIL, pipelineDetailHandler, renderRunRoutes, THREAD_ID } from './testUtils'

// RunDetailPage mounts useRunLiveness — pin the stream to idle (same boundary
// mock as RunDetailPage.test.tsx).
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

// CodeMirror needs layout APIs jsdom lacks (PhaseWorkspace prompt tab).
vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('pre', { 'data-testid': 'codemirror' }, value),
  }
})

/** Pressed phase toggles inside the open pre-flight modal. */
async function pressedPhases(): Promise<string[]> {
  const strip = await screen.findByRole('group', { name: 'Phases to run' })
  return within(strip)
    .getAllByRole('button', { pressed: true })
    .map((button) => button.textContent ?? '')
}

describe('RunsListPage row overflow menu (entry point 1)', () => {
  function renderRunsPage() {
    const router = createMemoryRouter(
      [
        { path: '/runs', element: <RunsListPage /> },
        { path: '/runs/:threadId', element: <div data-testid="run-detail-probe" /> },
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

  it('opens the pre-flight modal from the row menu WITHOUT triggering row navigation', async () => {
    server.use(
      pipelinesHandler(PIPELINES_FIXTURE).handler,
      // The modal fetches this thread's state on open.
      http.get(`*/v1/pipelines/${RUN_BUSY.thread_id}`, () =>
        HttpResponse.json({ ...PIPELINE_DETAIL, thread_id: RUN_BUSY.thread_id }),
      ),
    )
    const user = userEvent.setup()
    const router = renderRunsPage()

    const row = await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`)
    await user.click(
      within(row).getByRole('button', { name: 'Run actions: Checkout latency regression' }),
    )
    // Opening the menu did not navigate.
    expect(router.state.location.pathname).toBe('/runs')

    await user.click(screen.getByRole('menuitem', { name: 'Re-run…' }))
    expect(router.state.location.pathname).toBe('/runs')
    expect(screen.queryByTestId('run-detail-probe')).not.toBeInTheDocument()

    // Modal is up and hydrates its default selection from the fetched plan.
    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()
    expect((await pressedPhases()).length).toBeGreaterThan(0)
  })

  it('row menu [Open] navigates to the run', async () => {
    server.use(pipelinesHandler(PIPELINES_FIXTURE).handler)
    const user = userEvent.setup()
    const router = renderRunsPage()

    const row = await screen.findByTestId(`runs-row-${RUN_BUSY.thread_id}`)
    await user.click(
      within(row).getByRole('button', { name: 'Run actions: Checkout latency regression' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Open' }))
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/runs/${RUN_BUSY.thread_id}`),
    )
  })
})

describe('RunDetailPage header split button (entry point 2)', () => {
  it('Re-run ▾ menu offers All phases / Run phases… and preselects accordingly', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    await screen.findByRole('group', { name: 'Phase progress' })
    await user.click(screen.getByRole('button', { name: 'Re-run options' }))
    expect(
      within(screen.getByRole('menu')).getAllByRole('menuitem').map((el) => el.textContent),
    ).toEqual(['All phases', 'Run phases…'])

    await user.click(screen.getByRole('menuitem', { name: 'All phases' }))
    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()
    expect(await pressedPhases()).toHaveLength(7)
  })

  it('the main Re-run segment opens the modal with all phases preselected', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    await screen.findByRole('group', { name: 'Phase progress' })
    await user.click(screen.getByRole('button', { name: 'Re-run' }))
    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()
    expect(await pressedPhases()).toHaveLength(7)
  })

  it('closes pre-flight state when a cached thread route replaces the run', async () => {
    server.use(
      http.get('*/v1/pipelines/:threadId', ({ params }) =>
        HttpResponse.json({
          ...PIPELINE_DETAIL,
          thread_id: String(params.threadId),
          title: `Run ${String(params.threadId)}`,
        }),
      ),
    )
    const user = userEvent.setup()
    const { router } = renderRunRoutes(['/runs/thread-2/phases/execution'])

    await screen.findByText('Run thread-2')
    await act(async () => router.navigate('/runs/thread-1/phases/execution'))
    await screen.findByText('Run thread-1')
    await user.click(screen.getByRole('button', { name: 'Re-run' }))
    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()

    await act(async () => router.navigate('/runs/thread-2/phases/execution'))
    await screen.findByText('Run thread-2')
    expect(screen.queryByRole('dialog', { name: 'Re-run phases' })).not.toBeInTheDocument()
  })
})
