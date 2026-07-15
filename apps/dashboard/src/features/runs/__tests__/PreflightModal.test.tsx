import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { http, HttpResponse } from 'msw'

import { QueryClientProvider } from '@tanstack/react-query'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

import { PreflightModal } from '../PreflightModal'
import { ALL_GATED_GATES } from '../useRerun'
import { PIPELINE_DETAIL, pipelineDetailHandler, THREAD_ID } from './testUtils'

interface CapturedRerun {
  phases: string[]
  gates_mode: 'inherit' | 'gated' | 'auto'
  idempotency_key: string
}

let rerunBodies: CapturedRerun[] = []

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
const EFFECTIVE_CONFIG = {
  assistant_id: 'asst-gold',
  project_id: 'proj-alpha',
  app_id: 'app-checkout',
  environment_id: 'env-stage',
  engine: 'loadrunner',
  connections: { execution: 'conn-loadrunner' },
  prompt_overrides: { 'phase/reporting': { version_id: 'ver-9' } },
  model_by_phase: { reporting: 'claude-sonnet' },
  agent_backend: 'anthropic',
  limits: { max_revise_loops: 5, poll_interval_s: 10 },
  gates: ALL_GATED_GATES,
}

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
    run_config: EFFECTIVE_CONFIG,
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
    rerunBodies = []
    server.use(pipelineDetailHandler(DETAIL_IDLE))
    server.use(
      http.post('*/v1/pipelines/:threadId/rerun', async ({ request }) => {
        rerunBodies.push((await request.json()) as CapturedRerun)
        return HttpResponse.json({ run_id: 'run-9' }, { status: 202 })
      }),
    )
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

  it('Start sends only phase/gate overrides to the trusted rerun facade', async () => {
    const user = userEvent.setup()
    const { router, onClose } = renderModal(['script_scenario', 'execution'])

    await screen.findByRole('group', { name: 'Phases to run' })
    await user.click(screen.getByRole('button', { name: 'Start phases' }))

    await waitFor(() => expect(rerunBodies).toHaveLength(1))
    expect(rerunBodies[0]).toEqual({
      phases: ['script_scenario', 'execution'],
      gates_mode: 'inherit',
      idempotency_key: expect.any(String),
    })

    await waitFor(() => expect(router.state.location.pathname).toBe(`/runs/${THREAD_ID}`))
    expect(router.state.location.search).toBe('?tab=log')
    expect(onClose).toHaveBeenCalled()
  })

  it.each([
    { mode: 'All auto', gatesMode: 'auto' },
    { mode: 'All gated', gatesMode: 'gated' },
  ])('gates mode "$mode" sends only the selected facade mode', async ({ mode, gatesMode }) => {
    const user = userEvent.setup()
    renderModal(['story_analysis'])

    await screen.findByRole('group', { name: 'Phases to run' })
    await user.click(within(screen.getByRole('group', { name: 'Gates mode' })).getByText(mode))
    await user.click(screen.getByRole('button', { name: 'Start phases' }))

    await waitFor(() => expect(rerunBodies).toHaveLength(1))
    expect(rerunBodies[0]).toEqual({
      phases: ['story_analysis'],
      gates_mode: gatesMode,
      idempotency_key: expect.any(String),
    })
  })

  it('keeps the modal open with an inline error when the rerun fails', async () => {
    server.use(
      http.post('*/v1/pipelines/:threadId/rerun', () =>
        HttpResponse.json(
          { title: 'rerun_already_active', detail: 'multitask reject', status: 409 },
          { status: 409 },
        ),
      ),
    )
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
