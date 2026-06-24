import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { QueryClientProvider } from '@tanstack/react-query'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { ALL_AUTO_GATES } from '../launchRun'
import { PreflightModal } from '../PreflightModal'
import { ALL_GATED_GATES } from '../useRerun'
import { PIPELINE_DETAIL, pipelineDetailHandler, THREAD_ID } from './testUtils'

const { runsCreate } = vi.hoisted(() => ({ runsCreate: vi.fn() }))

// The rerun path goes through the SDK client factory — fake runs.create.
vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () => Promise.resolve({ runs: { create: runsCreate } }),
}))

const FULL_PLAN = [
  'story_analysis',
  'test_planning',
  'env_triage',
  'script_scenario',
  'execution',
  'reporting',
  'postmortem',
]

const recent = new Date(Date.now() - 60_000).toISOString()

/**
 * Idle thread, full last plan, everything OK while all 7 stay selected:
 * story_analysis/test_planning succeeded recently (reuse when unchecked),
 * execution FAILED (unchecking it blocks reporting).
 */
const DETAIL_IDLE: PipelineDetail = {
  ...PIPELINE_DETAIL,
  thread_status: 'idle',
  pending_gate: null,
  interrupts: [],
  values: {
    title: 'Re-run fixture',
    phases_plan: FULL_PLAN,
    phase_results: {
      story_analysis: { phase: 'story_analysis', status: 'succeeded', attempt: 2, ended_at: recent },
      test_planning: { phase: 'test_planning', status: 'succeeded', attempt: 1, ended_at: recent },
      script_scenario: { phase: 'script_scenario', status: 'succeeded', attempt: 1, ended_at: recent },
      execution: { phase: 'execution', status: 'failed', attempt: 1, ended_at: recent },
    },
  },
}

function renderModal(initialSelection?: Parameters<typeof PreflightModal>[0]['initialSelection']) {
  const onClose = vi.fn()
  const router = createMemoryRouter(
    [
      {
        path: '/',
        element: (
          <PreflightModal
            threadId={THREAD_ID}
            initialSelection={initialSelection}
            onClose={onClose}
          />
        ),
      },
      { path: '/runs/:threadId', element: <div data-testid="run-page" /> },
    ],
    { initialEntries: ['/'] },
  )
  render(
    <QueryClientProvider client={createTestQueryClient()}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { router, onClose }
}

const toggleByName = (name: string) => screen.getByRole('button', { name, pressed: true })

describe('PreflightModal', () => {
  beforeEach(() => {
    runsCreate.mockReset().mockResolvedValue({ run_id: 'run-9' })
    server.use(pipelineDetailHandler(DETAIL_IDLE))
  })

  it('loads thread state, pre-checks the last plan, and recomputes readiness as toggles change', async () => {
    const user = userEvent.setup()
    renderModal()

    const dialog = screen.getByRole('dialog', { name: 'Re-run phases' })
    // Loading state inside the modal while the snapshot fetch is in flight.
    expect(within(dialog).getByRole('status', { name: 'Loading thread state' })).toBeInTheDocument()

    // Defaults to the last resolved plan: all 7 pressed, every row OK.
    const strip = await screen.findByRole('group', { name: 'Phases to run' })
    expect(within(strip).getAllByRole('button', { pressed: true })).toHaveLength(7)
    const readiness = screen.getByRole('list', { name: 'Plan readiness' })
    expect(within(readiness).getAllByRole('listitem')).toHaveLength(7)
    expect(within(readiness).queryByText(/Will reuse/)).not.toBeInTheDocument()

    // Live update: uncheck story_analysis -> test_planning flips to REUSE.
    await user.click(toggleByName('Story Analysis'))
    const testPlanningRow = within(readiness)
      .getByText('Will reuse Story Analysis artifacts (attempt 2, 1m ago).')
      .closest('li')
    expect(testPlanningRow).toHaveAttribute('data-phase', 'test_planning')
    expect(testPlanningRow).toHaveAttribute('data-level', 'reuse')
    expect(within(readiness).getAllByRole('listitem')).toHaveLength(6)
  })

  it('shows the danger caption on blockers but keeps Start ENABLED (warn-don\'t-block)', async () => {
    const user = userEvent.setup()
    renderModal()

    await screen.findByRole('group', { name: 'Phases to run' })
    expect(screen.queryByText('server will reject at plan resolution')).not.toBeInTheDocument()

    // Uncheck execution: it failed on the thread, so reporting is blocked.
    await user.click(toggleByName('Execution'))
    const row = screen
      .getByText('Include Execution or it will fail at plan resolution.')
      .closest('li')
    expect(row).toHaveAttribute('data-level', 'blocked')
    expect(screen.getByText('server will reject at plan resolution')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start phases' })).toBeEnabled()
  })

  it('disables Start only when no phases are selected', async () => {
    const user = userEvent.setup()
    renderModal(['env_triage'])

    await screen.findByRole('group', { name: 'Phases to run' })
    expect(screen.getByRole('button', { name: 'Start phases' })).toBeEnabled()
    await user.click(toggleByName('Env Triage'))
    expect(screen.getByText('No phases selected.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start phases' })).toBeDisabled()
  })

  it('Start sends input {} (NOT null) + exact configurable, then navigates to ?tab=activity', async () => {
    const user = userEvent.setup()
    const { router, onClose } = renderModal(['script_scenario', 'execution'])

    await screen.findByRole('group', { name: 'Phases to run' })
    await user.click(screen.getByRole('button', { name: 'Start phases' }))

    await waitFor(() => expect(runsCreate).toHaveBeenCalledTimes(1))
    const [threadId, assistant, payload] = runsCreate.mock.calls[0] as [
      string,
      string,
      Record<string, unknown>,
    ]
    expect(threadId).toBe(THREAD_ID)
    expect(assistant).toBe('pipeline')
    expect(payload.input).toEqual({})
    expect(payload.input).not.toBeNull()
    // Inherit defaults: gates OMITTED so assistant/backend policy applies.
    expect(payload.config).toEqual({
      recursion_limit: expect.any(Number),
      configurable: { phases: ['script_scenario', 'execution'] },
    })
    expect(payload).toMatchObject({
      streamResumable: true,
      durability: 'sync',
      multitaskStrategy: 'reject',
    })

    await waitFor(() => expect(router.state.location.pathname).toBe(`/runs/${THREAD_ID}`))
    expect(router.state.location.search).toBe('?tab=activity')
    expect(onClose).toHaveBeenCalled()
  })

  it.each([
    { mode: 'All auto', gates: ALL_AUTO_GATES },
    { mode: 'All gated', gates: ALL_GATED_GATES },
  ])('gates mode "$mode" sends the uniform gates map', async ({ mode, gates }) => {
    const user = userEvent.setup()
    renderModal(['story_analysis'])

    await screen.findByRole('group', { name: 'Phases to run' })
    await user.click(within(screen.getByRole('group', { name: 'Gates mode' })).getByText(mode))
    await user.click(screen.getByRole('button', { name: 'Start phases' }))

    await waitFor(() => expect(runsCreate).toHaveBeenCalledTimes(1))
    const payload = runsCreate.mock.calls[0]?.[2] as Record<string, unknown>
    expect(payload.config).toEqual({
      recursion_limit: expect.any(Number),
      configurable: { phases: ['story_analysis'], gates },
    })
  })

  it('keeps the modal open with an inline error when the rerun fails', async () => {
    runsCreate.mockRejectedValue(new Error('multitask reject'))
    const user = userEvent.setup()
    const { onClose } = renderModal(['story_analysis'])

    await screen.findByRole('group', { name: 'Phases to run' })
    await user.click(screen.getByRole('button', { name: 'Start phases' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Re-run failed: multitask reject')
    expect(screen.getByRole('dialog', { name: 'Re-run phases' })).toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })
})
