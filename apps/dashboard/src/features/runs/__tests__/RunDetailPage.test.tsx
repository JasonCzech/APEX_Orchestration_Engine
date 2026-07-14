import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

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
  it('redirects /runs/:threadId to the current phase', async () => {
    server.use(pipelineDetailHandler())
    const { router } = renderRunRoutes([`/runs/${THREAD_ID}`])

    await screen.findByRole('tablist', { name: 'Phase workspace tabs' })
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(`/runs/${THREAD_ID}/phases/reporting`),
    )
  })

  it('renders phase progress without the duplicate pipeline context sidebar', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    const progress = await screen.findByRole('group', { name: 'Phase progress' })
    expect(screen.queryByRole('navigation', { name: 'Pipeline phases' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Pipeline context/i)).not.toBeInTheDocument()
    expect(
      within(progress).getByRole('button', { name: 'test_planning — succeeded (attempt 2)' }),
    ).toBeInTheDocument()
    expect(
      within(progress).getByRole('button', { name: 'env_triage — skipped (attempt 1)' }),
    ).toBeInTheDocument()
    expect(
      within(progress).getByRole('button', { name: 'reporting — running (attempt 1)' }),
    ).toBeInTheDocument()
  })

  it('switches workspace tabs via ?tab=', async () => {
    server.use(pipelineDetailHandler())
    const user = userEvent.setup()
    const { router } = renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning?tab=reasoning`])

    // Deep link lands on the reasoning tab.
    expect(await screen.findByText('Tighten the ramp to 5 minutes.')).toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: 'Phase Details' }))
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
    expect(chip).toHaveTextContent('catalog · story_analysis@v3')
    expect(screen.getAllByTestId('codemirror')[0]).toHaveTextContent(
      'You are the story analysis agent.',
    )
  })

  it('renders the reasoning empty state for phases without reasoning details', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=reasoning`])

    expect(await screen.findByText('No reasoning details recorded for this phase yet.')).toBeInTheDocument()
  })

  it('renders execution KPI pills and the passed badge from test_summary', async () => {
    server.use(pipelineDetailHandler())
    // Busy threads default to Pipeline Log, so pin ?tab=details here.
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])

    const row = await screen.findByTestId('kpi-row')
    expect(within(row).getByText('42.5')).toBeInTheDocument()
    expect(within(row).getByText('212 ms')).toBeInTheDocument()
    expect(within(row).getByText('0.42%')).toBeInTheDocument()
    expect(within(row).getByText('50')).toBeInTheDocument()
    expect(within(row).getByText('Passed')).toBeInTheDocument()
  })

  it('shows the pending-gate slim banner with a live Review link from non-gate phases (D3)', async () => {
    server.use(pipelineDetailHandler(PIPELINE_DETAIL_INTERRUPTED))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=details`])

    const banner = await screen.findByTestId('gate-slim-banner')
    expect(banner).toHaveTextContent('Phase review gate open on Reporting')
    expect(banner).toHaveTextContent('Reporting')
    expect(within(banner).getByRole('link', { name: 'Review' })).toHaveAttribute(
      'href',
      `/runs/${THREAD_ID}/phases/reporting`,
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
})
