import { cloneElement, isValidElement, type ReactNode } from 'react'
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'
import { ENGINE_RETRY_KINDS } from '@apex/pipeline-events'

import { bumpSessionRevision } from '@/auth/keyStorage'
import { server } from '@/test/server'
import { queryKeys } from '@/api/queryKeys'
import type { ThreadStateSnapshot } from '@/api/hooks/useThreadState'

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

// Running-phase regressions also render the live metrics container in jsdom.
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return { ...actual, ResponsiveContainer: ({ children }: { children: ReactNode }) =>
    isValidElement(children)
      ? cloneElement(children as React.ReactElement<{ width?: number; height?: number }>, { width: 600, height: 180 })
      : children,
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

  it.each(['resolved', 'missing', 'unavailable'] as const)('uses one application label in scope and run context (%s)', async catalog => {
    const detail = { ...structuredClone(PIPELINE_DETAIL), app_id: 'application-uuid' }
    server.use(pipelineDetailHandler(detail), http.get('*/v1/catalog/applications', () =>
      catalog === 'unavailable' ? HttpResponse.json({ detail: 'Catalog unavailable' }, { status: 503 })
        : HttpResponse.json(catalog === 'resolved' ? [{ id: detail.app_id, name: 'ACC' }] : [])))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    const scope = screen.getByRole('navigation', { name: 'Current run scope' })
    const expected = catalog === 'resolved' ? 'ACC' : detail.app_id
    const context = within(overview).getByRole('heading', { name: 'Run context' }).parentElement
    if (!context) throw new Error('Run context must be inside its panel')
    expect(await within(context).findByText(expected, { exact: true })).toHaveAttribute('title', detail.app_id)
    expect(await within(scope).findByText(expected, { exact: true })).toHaveAttribute('title', detail.app_id)
  })

  it('keeps the full application identity available to copy behind technical details', async () => {
    const user = userEvent.setup()
    const detail = { ...structuredClone(PIPELINE_DETAIL), app_id: 'application-uuid-with-full-identity' }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByRole('button', { name: 'Copy application ID' })).not.toBeVisible()
    await user.click(within(overview).getByText('Technical identifiers'))
    await user.click(within(overview).getByRole('button', { name: 'Copy application ID' }))
    expect(await navigator.clipboard.readText()).toBe(detail.app_id)
    expect(within(overview).getByText('application ID copied')).toBeInTheDocument()
  })

  it('uses the matching interrupt when gate summary metadata is absent', async () => {
    const detail = { ...structuredClone(PIPELINE_DETAIL_INTERRUPTED),
      pending_gate: { interrupt_id: 'int-1', kind: null, phase: null } }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    await waitFor(() => expect(screen.getByRole('status', { name: 'Run updates' })).toHaveTextContent('Review phase results open on Reporting.'))
    expect(screen.getByRole('status', { name: 'Run updates' })).not.toHaveTextContent('null')
  })

  it('announces canonical same-id review updates but leaves unchanged polls silent', async () => {
    const detail = structuredClone(PIPELINE_DETAIL_INTERRUPTED)
    const interrupt = detail.interrupts?.[0]
    if (!interrupt) throw new Error('The fixture must have a review interrupt')
    server.use(pipelineDetailHandler(detail))
    const { queryClient } = renderRunRoutes([`/runs/${THREAD_ID}`])
    const region = await screen.findByRole('status', { name: 'Run updates' })
    await waitFor(() => expect(region).toHaveTextContent('Review phase results open on Reporting.'))
    const mutations: MutationRecord[] = []
    const observer = new MutationObserver(records => mutations.push(...records))
    observer.observe(region, { childList: true, subtree: true, characterData: true })
    const refresh = async () => {
      mutations.length = 0
      await act(async () => {
        await queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(THREAD_ID) })
      })
    }
    try {
      detail.updated_at = '2026-06-01T10:12:00+00:00'
      await refresh()
      await waitFor(() => expect(region).toHaveTextContent('Review phase results updated on Reporting.'))
      expect(mutations.length).toBeGreaterThan(0)
      await refresh()
      expect(mutations).toHaveLength(0)

      interrupt.payload = {
        ...interrupt.payload,
        summary: 'Revised report compiled with additional evidence.',
      }
      await refresh()
      await waitFor(() => expect(mutations.length).toBeGreaterThan(0))
      expect(region).toHaveTextContent('Review phase results updated on Reporting.')
      await refresh()
      expect(mutations).toHaveLength(0)
    } finally {
      observer.disconnect()
    }
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

  it('retains valid dialogue without rendering malformed content after schema drift', async () => {
    const detail = structuredClone(PIPELINE_DETAIL)
    detail.values = { ...detail.values, dialogue: [
      { id: 'valid-dialogue', phase: 'test_planning', role: 'operator', content: 'Keep the valid discussion.' },
      { id: 'invalid-dialogue', phase: 'test_planning', role: 'agent', content: { unexpected: 'object' } },
    ] }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning?tab=reasoning`])
    expect(await screen.findByText('Keep the valid discussion.')).toBeInTheDocument()
    expect(screen.queryByText('Unexpected Application Error!')).not.toBeInTheDocument()
  })

  it('validates nested engine and review fields while preserving valid siblings after drift', async () => {
    const detail = structuredClone(PIPELINE_DETAIL)
    const validPrompt = { system: 'Valid system', phase_prompt: 'Valid phase prompt', application: null,
      additional_context: '', source: { origin: 'catalog' }, updated_at: '2026-10-17T00:00:00Z', updated_by: 'operator' }
    const validApplication = { content: 'Valid application', source: { origin: 'catalog' },
      updated_at: '2026-10-17T00:00:00Z', updated_by: 'operator' }
    detail.values = { ...detail.values,
      engine_handle: { engine: 'apex_load', application_id: { unexpected: 'object' }, release_id: 'release-valid',
        extras: { mapping_revision: { unexpected: 'object' } }, console_url: 'https://load.example/#/report/123' },
      prompt_reviews: { story_analysis: validPrompt, test_planning: { ...validPrompt, system: { unexpected: 'object' } } },
      application_reviews: { valid: validApplication, invalid: { ...validApplication, content: { unexpected: 'object' } } },
    }
    server.use(pipelineDetailHandler(detail))
    const { queryClient } = renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByText('release-valid')).toBeInTheDocument()
    expect(within(overview).getByRole('link', { name: 'Open load execution' })).toHaveAttribute('href', expect.stringContaining('#/report/123'))
    const snapshot = queryClient.getQueryData<ThreadStateSnapshot>(queryKeys.threads.state(THREAD_ID))
    expect(snapshot?.stateParseFailed).toBe(true)
    expect(snapshot?.state.engine_handle?.application_id).toBeUndefined()
    expect(snapshot?.state.engine_handle?.extras).toBeUndefined()
    expect(snapshot?.state.prompt_reviews).toEqual({ story_analysis: validPrompt })
    expect(snapshot?.state.application_reviews).toEqual({ valid: validApplication })
    expect(screen.queryByText('Unexpected Application Error!')).not.toBeInTheDocument()
  })

  it('describes absent bounded evidence as unavailable across overview and phase details', async () => {
    const detail = { ...structuredClone(PIPELINE_DETAIL), thread_status: 'error', execution_result: null,
      current_phase: 'test_planning', phase_strip: [{ phase: 'test_planning', status: 'failed', attempt: 1 }],
      values: { projection_truncated: true, phases_plan: ['test_planning'], current_phase: 'test_planning',
        phase_results: { test_planning: { status: 'failed', attempt: 1 } } },
    }
    server.use(pipelineDetailHandler(detail))
    const { router } = renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    expect(within(overview).getByText(/Execution evidence is unavailable in this preview\./)).toBeInTheDocument()
    expect(within(overview).getByText('The failure or stop reason is unavailable in this preview.')).toBeInTheDocument()
    expect(within(overview).getByText('Artifact details are unavailable in this preview.')).toBeInTheDocument()
    expect(within(overview).queryByText('Not applicable')).not.toBeInTheDocument()
    expect(within(overview).queryByText('No failure or stop reason was recorded.')).not.toBeInTheDocument()
    expect(within(overview).queryByText('No artifacts were recorded for this run.')).not.toBeInTheDocument()
    await act(async () => { await router.navigate(`/runs/${THREAD_ID}/phases/test_planning?tab=details`) })
    const phase = await screen.findByRole('region', { name: 'Phase snapshot' })
    expect(within(phase).getByText('The phase summary is unavailable in this preview.')).toBeInTheDocument()
    expect(within(phase).getByText('Unavailable in this preview', { exact: true })).toBeInTheDocument()
    expect(within(phase).queryByText('No phase artifacts')).not.toBeInTheDocument()
    expect(screen.getAllByText('Output source: Unavailable in this preview')).toHaveLength(2)
  })

  it('marks omitted phase evidence unavailable when only the phase preview was truncated', async () => {
    const detail = structuredClone(PIPELINE_DETAIL)
    detail.values = { phases_plan: ['test_planning'], current_phase: 'test_planning',
      phase_results: { test_planning: { status: 'awaiting_output_review', attempt: 1, projection_truncated: true } } }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning?tab=details`])
    const phase = await screen.findByRole('region', { name: 'Phase snapshot' })
    expect(within(phase).getByText('The phase summary is unavailable in this preview.')).toBeInTheDocument()
    expect(within(phase).getByText('Unavailable in this preview', { exact: true })).toBeInTheDocument()
    expect(within(phase).queryByText('No phase artifacts')).not.toBeInTheDocument()
    expect(screen.getAllByText('Output source: Unavailable in this preview')).toHaveLength(2)
  })

  it.each([false, true])('discloses a bounded snapshot with unrelated schema drift (%s)', async drift => {
    const detail = structuredClone(PIPELINE_DETAIL)
    detail.values = { projection_truncated: true, current_phase: 'test_planning', ...(drift ? { title: 42 } : {}) }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning?tab=reasoning`])
    expect(await screen.findByRole('note')).toHaveTextContent('The displayed history may be incomplete.')
    expect(await screen.findByText('Reasoning details are unavailable in this preview.')).toBeInTheDocument()
    expect(screen.queryByText('No reasoning details recorded for this phase.')).not.toBeInTheDocument()
  })

  it.each([
    [{ uri: 'https://load.example/#/report/123' }, true],
    [['not an evidence object'], false],
    ['invalid evidence', false],
  ])('preserves valid imported evidence after unrelated schema drift (%j)', async (externalResults, hasExecutionReference) => {
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
    expect(within(overview).getByText('Not available')).toBeInTheDocument()
    expect(within(overview).queryByText('Not applicable')).not.toBeInTheDocument()
    if (hasExecutionReference) {
      expect(within(overview).getByText(/An execution reference is recorded; collected execution evidence is unavailable\./)).toBeInTheDocument()
      expect(within(overview).queryByText(/Execution evidence has not been recorded\./)).not.toBeInTheDocument()
      expect(within(overview).getByRole('link', { name: 'Open load execution' })).toHaveAttribute('href', expect.stringContaining('#/report/123'))
    }
  })

  it('uses the same unavailable verdict in the header and overview when execution was not selected', async () => {
    const detail = { ...structuredClone(PIPELINE_DETAIL), execution_result: null,
      phase_strip: [{ phase: 'reporting', status: 'succeeded' }],
      values: { phases_plan: ['reporting'], phase_results: { reporting: { status: 'succeeded' } } } }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}`])
    const overview = await screen.findByRole('region', { name: 'Run overview' })
    const acceptance = within(overview).getByRole('heading', { name: 'Test acceptance' }).parentElement!
    expect(within(acceptance).getByText('Not available', { exact: true })).toBeInTheDocument()
    expect(acceptance).toHaveTextContent('Execution was not selected; there is no test acceptance decision for this run.')
    expect(screen.getByText('Test result:').parentElement).toHaveTextContent('Test result: Not available')
    expect(screen.queryByText('Not applicable')).not.toBeInTheDocument()
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

  it.each(['pending', 'running', 'awaiting_output_review', 'failed', 'aborted'])('withholds execution KPI results from phase details while status is %s', async status => {
    const detail = structuredClone(PIPELINE_DETAIL)
    const state = detail.values as { phase_results: { execution: { status: string; test_summary: { passed: boolean } } } }
    state.phase_results.execution.status = status
    state.phase_results.execution.test_summary.passed = false
    detail.execution_result = null
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])
    await screen.findByRole('region', { name: 'Execution workspace' })
    expect(screen.queryByTestId('kpi-row')).not.toBeInTheDocument()
  })

  it.each([
    ['inconclusive', 'Inconclusive'], ['not_assessed', 'Not assessed'], ['unavailable', 'Not available'],
  ])('shows the structured %s assessment in completed phase details', async (status, label) => {
    const detail = structuredClone(PIPELINE_DETAIL)
    const state = detail.values as { phase_results: { execution: { test_summary: Record<string, unknown> } } }
    Object.assign(state.phase_results.execution.test_summary, { passed: null, outcome: {
      schema_version: 1, execution_id: 'execution-1', observed_at: '2026-10-17T00:00:00Z',
      assessment: { status, reason_code: 'review_required', explanation: 'Recorded assessment.' },
      execution: { termination_reason: 'duration_completed' }, evidence: { status: 'partial' },
    } })
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])
    expect(within(await screen.findByTestId('kpi-row')).getByText(label!)).toBeInTheDocument()
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

  it.each(ENGINE_RETRY_KINDS)('does not relabel an engine recovery checkpoint as phase-result review: %s', async kind => {
    const detail = {
      ...structuredClone(PIPELINE_DETAIL),
      thread_status: 'interrupted',
      current_phase: 'execution',
      phase_strip: [{ phase: 'execution', status: 'running', attempt: 1 }],
      values: {
        phases_plan: ['execution'],
        phase_results: { execution: { status: 'running', attempt: 1 } },
      },
      pending_gate: { interrupt_id: 'recovery-1', kind, phase: 'execution' },
      interrupts: [{
        interrupt_id: 'recovery-1', kind, phase: 'execution',
        payload: {
          schema_version: 1, kind, phase: 'execution', attempt: 1,
          thread_id: THREAD_ID, actions: ['retry'], message: 'Provider recovery required.',
        },
      }],
    }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/reporting?tab=details`])
    // Wait for the gate machine to hydrate; the first snapshot render alone
    // does not exercise the page-level pending-interrupt override.
    await screen.findByTestId('gate-slim-banner')
    const phases = screen.getByRole('group', { name: 'Phase progress' })
    expect(within(phases).getByRole('button', { name: 'Execution — Executing (attempt 1)' })).toBeInTheDocument()
    expect(within(phases).queryByRole('button', { name: /Execution — Awaiting output review/ })).not.toBeInTheDocument()
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
