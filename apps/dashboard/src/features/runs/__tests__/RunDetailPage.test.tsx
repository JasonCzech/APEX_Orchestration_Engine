import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

import {
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

  it('renders rail statuses, attempt badges, warning chips, and skipped styling', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`])

    const rail = await screen.findByRole('navigation', { name: 'Pipeline phases' })
    const planning = within(rail).getByText('Test Planning').closest('a')
    expect(planning).toHaveAttribute('data-status', 'succeeded')
    expect(within(planning as HTMLElement).getByText('×2')).toBeInTheDocument()
    expect(within(planning as HTMLElement).getByText('⚠ 1')).toBeInTheDocument()

    const triage = within(rail).getByText('Env Triage').closest('a')
    expect(triage).toHaveAttribute('data-status', 'skipped')
    expect(triage?.className).toContain('skipped')

    const reporting = within(rail).getByText('Reporting').closest('a')
    expect(reporting).toHaveAttribute('data-status', 'running')
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
})
