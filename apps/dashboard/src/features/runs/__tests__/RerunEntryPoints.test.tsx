/**
 * The three phase-subset re-run entry points (plan Part 2 §4): PhaseRail
 * kebab, runs-grid row overflow menu, run-detail header split button. All
 * funnel into PreflightModal with the documented preselection.
 */
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import type { PipelineState } from '@apex/pipeline-events'

import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { PhaseRail } from '../PhaseRail'
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

/** Last plan SKIPS env_triage — exercises the "run from here" intersect. */
const RAIL_STATE: PipelineState = {
  phases_plan: ['story_analysis', 'test_planning', 'script_scenario', 'execution'],
  phase_results: {
    story_analysis: { phase: 'story_analysis', status: 'succeeded', attempt: 1 },
    test_planning: { phase: 'test_planning', status: 'succeeded', attempt: 1 },
  },
}

function renderRail(state: PipelineState = RAIL_STATE) {
  const router = createMemoryRouter(
    [
      {
        path: '/runs/:threadId/phases/:phase',
        element: <PhaseRail threadId={THREAD_ID} state={state} />,
      },
    ],
    { initialEntries: [`/runs/${THREAD_ID}/phases/execution`] },
  )
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

/** Pressed phase toggles inside the open pre-flight modal. */
async function pressedPhases(): Promise<string[]> {
  const strip = await screen.findByRole('group', { name: 'Phases to run' })
  return within(strip)
    .getAllByRole('button', { pressed: true })
    .map((button) => button.textContent ?? '')
}

describe('PhaseRail kebab (entry point 1)', () => {
  it('opens a menu per phase row and closes on Escape with focus restored', async () => {
    const user = userEvent.setup()
    renderRail()

    const trigger = screen.getByRole('button', { name: 'Phase actions: Test Planning' })
    await user.click(trigger)
    const menu = screen.getByRole('menu', { name: 'Phase actions: Test Planning' })
    expect(within(menu).getAllByRole('menuitem').map((el) => el.textContent)).toEqual([
      'Re-run this phase',
      'Run from here',
      'Run phases…',
    ])

    await user.keyboard('{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    // Escape did not open the modal.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('closes the menu on outside click', async () => {
    const user = userEvent.setup()
    renderRail()

    await user.click(screen.getByRole('button', { name: 'Phase actions: Execution' }))
    expect(screen.getByRole('menu')).toBeInTheDocument()
    await user.click(screen.getByText('Phases')) // rail heading = outside the menu
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
  })

  it('"Re-run this phase" opens the modal preselecting only that phase', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    renderRail()

    await user.click(screen.getByRole('button', { name: 'Phase actions: Execution' }))
    await user.click(screen.getByRole('menuitem', { name: 'Re-run this phase' }))

    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()
    expect(await pressedPhases()).toEqual(['Execution'])
  })

  it('"Run from here" preselects the phase + downstream phases of the LAST plan', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    renderRail()

    await user.click(screen.getByRole('button', { name: 'Phase actions: Test Planning' }))
    await user.click(screen.getByRole('menuitem', { name: 'Run from here' }))

    // env_triage was not in the last plan -> stays unchecked.
    expect(await pressedPhases()).toEqual(['Test Planning', 'Script & Scenario', 'Execution'])
  })

  it('"Run phases…" preselects the full last plan', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    renderRail()

    await user.click(screen.getByRole('button', { name: 'Phase actions: Postmortem' }))
    await user.click(screen.getByRole('menuitem', { name: 'Run phases…' }))

    expect(await pressedPhases()).toEqual([
      'Story Analysis',
      'Test Planning',
      'Script & Scenario',
      'Execution',
    ])
  })
})

describe('RunsListPage row overflow menu (entry point 2)', () => {
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

describe('RunDetailPage header split button (entry point 3)', () => {
  it('Re-run ▾ menu offers All phases / Run phases… and preselects accordingly', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    await screen.findByRole('navigation', { name: 'Pipeline phases' })
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

    await screen.findByRole('navigation', { name: 'Pipeline phases' })
    await user.click(screen.getByRole('button', { name: 'Re-run' }))
    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()
    expect(await pressedPhases()).toHaveLength(7)
  })
})
