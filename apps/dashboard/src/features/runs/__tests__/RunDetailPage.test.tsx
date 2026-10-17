import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { bumpSessionRevision } from '@/auth/keyStorage'
import { server } from '@/test/server'
import { queryKeys } from '@/api/queryKeys'

import {
  PIPELINE_DETAIL,
  PIPELINE_DETAIL_INTERRUPTED,
  pipelineDetailHandler,
  renderRunRoutes,
  THREAD_ID,
} from './testUtils'

// CodeMirror needs real DOM measurement APIs jsdom lacks; the viewers' contract
// (value passed through) is what these tests assert.
vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({ value }: { value: string }) =>
      createElement('pre', { 'data-testid': 'codemirror' }, value),
  }
})

// D2: RunDetailPage mounts useRunLiveness. These D1 snapshot tests pin the
// stream to idle via the integration contract (never the module's internals)
// so no SSE/run-discovery requests fire against the msw server.
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

describe('RunDetailPage', () => {
  it('opens the outcome overview without redirecting', async () => {
    server.use(pipelineDetailHandler())
    const { router } = renderRunRoutes([`/runs/${THREAD_ID}`])

    await screen.findByRole('region', { name: 'Run overview' })
    expect(router.state.location.pathname).toBe(`/runs/${THREAD_ID}`)
    expect(screen.getByRole('link', { name: 'Open Reporting' })).toHaveAttribute('href', `/runs/${THREAD_ID}/phases/reporting`)
  })

  it.each([true, false])('explains a recorded acceptance verdict without notes (%s)', async passed => {
    const detail = structuredClone(PIPELINE_DETAIL)
    const state = detail.values as { phase_results: { execution: { test_summary: { passed: boolean; notes?: string | null } } } }
    state.phase_results.execution.test_summary.passed = passed
    delete state.phase_results.execution.test_summary.notes
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByText(passed ? 'Passed' : 'Failed')).toBeInTheDocument()
    expect(within(overview).queryByText(/Acceptance has not been assessed/)).not.toBeInTheDocument()
    expect(within(overview).getByText(passed
      ? 'Recorded execution evidence meets the configured acceptance criteria.'
      : 'Recorded execution evidence does not meet the configured acceptance criteria.')).toBeInTheDocument()
  })

  it('keeps an unassessed execution distinct from a stale summary verdict', async () => {
    const detail = structuredClone(PIPELINE_DETAIL)
    detail.execution_result = 'passed'
    const state = detail.values as { phase_results: { execution: { test_summary: { passed: boolean | null; notes?: string | null } } } }
    state.phase_results.execution.test_summary.passed = null
    delete state.phase_results.execution.test_summary.notes
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByText('Not available')).toBeInTheDocument()
    expect(within(overview).queryByText('Passed')).not.toBeInTheDocument()
  })

  it('uses recorded phase coverage when state fields have schema drift', async () => {
    const detail = { ...structuredClone(PIPELINE_DETAIL), values: { phase_results: 'unreadable' } }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByText(/4\/6 phases succeeded · 1 skipped/)).toBeInTheDocument()
  })

  it.each([
    [{ uri: 'https://load.example/#/report/123' }, 'Not available'],
    [['not an evidence object'], 'Not applicable'],
    ['invalid evidence', 'Not applicable'],
  ])('preserves valid imported evidence after unrelated schema drift (%j)', async (externalResults, expectedAcceptance) => {
    const detail = {
      ...structuredClone(PIPELINE_DETAIL),
      execution_result: null,
      phase_strip: [{ phase: 'reporting', status: 'succeeded' }],
      values: {
        title: 42,
        phases_plan: ['reporting'],
        external_results: externalResults,
      },
    }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByText(expectedAcceptance)).toBeInTheDocument()
    if (expectedAcceptance === 'Not available') {
      expect(within(overview).queryByText('Not applicable')).not.toBeInTheDocument()
      expect(within(overview).getByText(/An execution reference is recorded; collected execution evidence is unavailable\./)).toBeInTheDocument()
      expect(within(overview).queryByText(/Execution evidence has not been recorded\./)).not.toBeInTheDocument()
      expect(within(overview).getByRole('link', { name: 'Open load execution' })).toHaveAttribute('href', expect.stringContaining('#/report/123'))
    }
  })

  it('uses the themed link style for Evidence Coverage artifacts', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}`])

    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByRole('link', { name: 'load-report.json' })).toHaveClass(
      'run-evidence-artifact-link',
    )
  })

  it('preserves legacy activity deep links to the pipeline log', async () => {
    server.use(pipelineDetailHandler())
    const { router } = renderRunRoutes([`/runs/${THREAD_ID}?tab=activity`])
    expect(await screen.findByRole('tab', { name: 'Agent log' })).toHaveAttribute('aria-selected', 'true')
    expect(router.state.location.pathname).toBe(`/runs/${THREAD_ID}/phases/reporting`)
  })

  it('offers a working recovery link for an unknown phase', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/invalid`])
    expect(await screen.findByRole('link', { name: 'Open run overview' })).toHaveAttribute('href', `/runs/${THREAD_ID}`)
  })

  it('renders phase progress without the duplicate pipeline context sidebar', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    const progress = await screen.findByRole('group', { name: 'Phase progress' })
    expect(screen.queryByRole('navigation', { name: 'Pipeline phases' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Pipeline context/i)).not.toBeInTheDocument()
    expect(
      within(progress).getByRole('button', { name: 'Test Planning — Complete (attempt 2)' }),
    ).toBeInTheDocument()
    expect(
      within(progress).getByRole('button', { name: 'Env Triage — Skipped (attempt 1)' }),
    ).toBeInTheDocument()
    expect(
      within(progress).getByRole('button', { name: 'Reporting — Executing (attempt 1)' }),
    ).toBeInTheDocument()
  })

  it('switches workspace tabs via ?tab=', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    const { router } = renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning?tab=reasoning`])

    // Deep link lands on the reasoning tab.
    expect(await screen.findByText('Tighten the ramp to 5 minutes.')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Phase details' }))
    expect(await screen.findByText('Planned 4 scenarios against the staging cluster.')).toBeInTheDocument()
    expect(router.state.location.search).toContain('tab=details')
  })

  it('links phase artifacts to the viewer route by artifact id', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])

    const link = (await screen.findByText('load-report.json')).closest('a')
    expect(link).not.toBeNull()
    expect(link).toHaveAttribute('href', `/runs/${THREAD_ID}/artifacts/exec-report`)
  })

  it('shows the resolved prompt with its provenance chip', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/story_analysis?tab=reasoning`])

    const chip = await screen.findByTestId('prompt-provenance')
    expect(chip).toHaveTextContent('Catalog · story_analysis@v3')
    expect(screen.getAllByTestId('codemirror')[0]).toHaveTextContent(
      'You are the story analysis agent.',
    )
  })

  it('links the transcript even when no other reasoning details are recorded', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=reasoning`])

    expect(await screen.findByRole('link', { name: 'Full agent transcript (attempt 1)' })).toHaveAttribute('href', `/runs/${THREAD_ID}/artifacts/execution-a1-transcript`)
  })

  it('renders execution KPI pills and the passed badge from test_summary', async () => {
    server.use(pipelineDetailHandler())
    // Busy threads default to Agent log, so pin ?tab=details here.
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])

    const row = await screen.findByTestId('kpi-row')
    expect(within(row).getByText('42.5')).toBeInTheDocument()
    expect(within(row).getByText('212 ms')).toBeInTheDocument()
    expect(within(row).getByText('0.42%')).toBeInTheDocument()
    expect(within(row).getByText('50')).toBeInTheDocument()
    expect(within(row).getByText('Passed')).toBeInTheDocument()
  })

  it('keeps unknown test outcomes distinct from pass and failure', async () => {
    const detail = structuredClone(PIPELINE_DETAIL)
    const state = detail.values as { phase_results: { execution: { test_summary: { passed: boolean | null } } } }
    state.phase_results.execution.test_summary.passed = null
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])
    const row = await screen.findByTestId('kpi-row')
    expect(within(row).getByText('Not available')).toBeInTheDocument()
    expect(within(row).queryByText('Passed')).not.toBeInTheDocument()
    expect(within(row).queryByText('Failed')).not.toBeInTheDocument()
  })

  it('shows the pending-gate slim banner with a live Review link from non-gate phases (D3)', async () => {
    server.use(pipelineDetailHandler(PIPELINE_DETAIL_INTERRUPTED))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])

    const banner = await screen.findByTestId('gate-slim-banner')
    expect(banner).toHaveTextContent('Review phase results open on Reporting')
    expect(banner).toHaveTextContent('Reporting')
    expect(within(banner).getByRole('link', { name: 'Review' })).toHaveAttribute(
      'href',
      `/runs/${THREAD_ID}/reviews/int-1?source=overview&phase=reporting`,
    )
  })

  it('surfaces a problem card with retry when the snapshot fails', async () => {
    server.use(
      http.get(`*/v1/pipelines/${THREAD_ID}`, () =>
        HttpResponse.json({ detail: 'thread not visible' }, { status: 403 }),
      ),
    )
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('thread not visible')
    expect(within(alert).getByRole('button', { name: 'Retry' })).toBeInTheDocument()
  })

  it('keeps the cached run usable when a background refresh fails', async () => {
    server.use(pipelineDetailHandler())
    const { queryClient } = renderRunRoutes([
      `/runs/${THREAD_ID}/phases/execution?tab=details`,
    ])
    expect(await screen.findByTestId('kpi-row')).toBeInTheDocument()

    server.use(
      http.get(`*/v1/pipelines/${THREAD_ID}`, () =>
        HttpResponse.json({ detail: 'temporary upstream failure' }, { status: 503 }),
      ),
    )
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(THREAD_ID) })
    })

    expect(await screen.findByText(/Showing cached data/)).toHaveTextContent(
      'temporary upstream failure',
    )
    expect(screen.getByTestId('kpi-row')).toBeInTheDocument()
    expect(screen.queryByText('Run failed to load')).not.toBeInTheDocument()
  })

  it('uses the engine kill switch even when the compact summary lacks engine metadata', async () => {
    const calls: string[] = []
    server.use(
      pipelineDetailHandler({
        ...PIPELINE_DETAIL,
        engine: null,
      }),
      http.post(`*/v1/engines/runs/${THREAD_ID}/abort`, async ({ request }) => {
        calls.push(request.url)
        return HttpResponse.json(
          {
            thread_id: THREAD_ID,
            engine: 'apex_load',
            external_run_id: 'load-42',
            cancelled_runs: ['run-1'],
          },
          { status: 202 },
        )
      }),
      http.post(`*/v1/pipelines/${THREAD_ID}/abort`, () => {
        calls.push('graph-only')
        return HttpResponse.json({ cancelled_run_ids: ['run-1'] }, { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    await user.click(await screen.findByRole('button', { name: 'Abort' }))
    await user.type(screen.getByLabelText('Type ABORT to confirm'), 'ABORT')
    await user.click(screen.getByRole('button', { name: 'Confirm abort' }))

    await waitFor(() => expect(calls).toHaveLength(1))
    expect(calls[0]).toContain(`/v1/engines/runs/${THREAD_ID}/abort`)
    expect(calls).not.toContain('graph-only')
  })

  it('renders engine abort failures instead of claiming the run stopped', async () => {
    server.use(
      pipelineDetailHandler({
        ...PIPELINE_DETAIL,
        engine: { engine: 'loadrunner', external_run_id: 'lr-9' },
      }),
      http.post(`*/v1/engines/runs/${THREAD_ID}/abort`, () =>
        HttpResponse.json({ detail: 'provider refused stop' }, { status: 502 }),
      ),
    )
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    await user.click(await screen.findByRole('button', { name: 'Abort' }))
    await user.type(screen.getByLabelText('Type ABORT to confirm'), 'ABORT')
    await user.click(screen.getByRole('button', { name: 'Confirm abort' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Abort failed: provider refused stop',
    )
  })

  it('does not run the graph abort fallback after the authenticated session changes', async () => {
    let graphAbortCalls = 0
    server.use(
      pipelineDetailHandler({
        ...PIPELINE_DETAIL,
        engine: null,
      }),
      http.post(`*/v1/engines/runs/${THREAD_ID}/abort`, () => {
        bumpSessionRevision()
        return HttpResponse.json({ detail: 'no external engine handle' }, { status: 404 })
      }),
      http.post(`*/v1/pipelines/${THREAD_ID}/abort`, () => {
        graphAbortCalls += 1
        return HttpResponse.json({ cancelled_run_ids: ['run-1'] }, { status: 202 })
      }),
    )
    const user = userEvent.setup()
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    await user.click(await screen.findByRole('button', { name: 'Abort' }))
    await user.type(screen.getByLabelText('Type ABORT to confirm'), 'ABORT')
    await user.click(screen.getByRole('button', { name: 'Confirm abort' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Abort failed: Authentication changed while the request was in flight.',
    )
    expect(graphAbortCalls).toBe(0)
  })
})
