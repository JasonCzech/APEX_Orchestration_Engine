import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

import { PipelineStateSchema } from '@apex/pipeline-events'

import { deriveTimeline } from '@/features/runs/timelineModel'
import { server } from '@/test/server'

import { PIPELINE_DETAIL_INTERRUPTED, PIPELINE_DETAIL, pipelineDetailHandler, renderRunRoutes, THREAD_ID } from './testUtils'

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

describe('TimelinePage', () => {
  it('resolves review actors and preserves their copyable recorded identities', async () => {
    const user = userEvent.setup()
    server.use(pipelineDetailHandler(), http.get('*/v1/pipelines/:threadId/actors', () => HttpResponse.json({ names: { 'ops@apex': 'Operations reviewer' }, truncated: false })))
    renderRunRoutes([`/runs/${THREAD_ID}/timeline`])
    const actors = await screen.findAllByText('Operations reviewer')
    expect(actors).toHaveLength(2)
    const event = actors[0]!.closest('li')!
    expect(event).toHaveTextContent('Gate prompt_review: approve')
    expect(event).toHaveTextContent('Story Analysis')
    await user.click(within(event).getByRole('button', { name: 'Copy Actor ID' }))
    expect(await navigator.clipboard.readText()).toBe('ops@apex')
  })

  it('does not fabricate a name when the recorded review actor is unknown', async () => {
    const detail = structuredClone(PIPELINE_DETAIL)
    const state = PipelineStateSchema.parse(detail.values)
    state.phase_results!.story_analysis!.approvals![0]!.actor = 'unknown'
    detail.values = state
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/timeline`])
    expect(await screen.findByText('Actor was not recorded')).toBeInTheDocument()
  })

  it('includes the current unaccepted review even without a durable phase result', async () => {
    const detail = structuredClone(PIPELINE_DETAIL_INTERRUPTED)
    detail.values = { phase_results: {} }
    server.use(pipelineDetailHandler(detail))
    renderRunRoutes([`/runs/${THREAD_ID}/timeline?phase=reporting`])
    expect(await screen.findByText('Reporting output awaiting review')).toBeInTheDocument()
    expect(screen.getByText('Review time not recorded')).toBeInTheDocument()
    expect(screen.getByRole('list')).toHaveTextContent('Draft report compiled; KPI deltas within tolerance.')
    expect(screen.queryByText('Reporting Complete')).not.toBeInTheDocument()
  })

  it('derives a chronologically ascending ledger from the snapshot', () => {
    const state = PipelineStateSchema.parse(PIPELINE_DETAIL.values)
    const events = deriveTimeline(state)

    const timestamps = events.map((event) => event.at)
    expect(timestamps).toEqual([...timestamps].sort())

    const labels = events.map((event) => event.label)
    expect(labels[0]).toBe('Story Analysis started')
    expect(labels.at(-1)).toBe('Reporting started')
    // Gate decision sits between phase start and phase end.
    expect(labels.indexOf('Gate prompt_review: approve')).toBeGreaterThan(
      labels.indexOf('Story Analysis started'),
    )
    expect(labels.indexOf('Gate prompt_review: approve')).toBeLessThan(
      labels.indexOf('Story Analysis Complete'),
    )
    // Engine lifecycle markers from the execution entry.
    expect(labels.indexOf('Engine started (sim)')).toBeLessThan(
      labels.indexOf('Engine summary collected — passed'),
    )
  })

  it('renders inside the shared workspace and preserves phase context', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/timeline?phase=execution&tab=reasoning`])

    const workspace = await screen.findByRole('region', { name: 'Run workspace' })
    expect(within(workspace).getByRole('heading', { name: 'Checkout latency regression' })).toBeInTheDocument()
    expect(within(workspace).getByRole('link', { name: 'Timeline' })).toHaveAttribute(
      'aria-current',
      'page',
    )
    expect(within(workspace).getByRole('link', { name: 'Workspace' })).toHaveAttribute(
      'href',
      `/runs/${THREAD_ID}/phases/execution?tab=reasoning`,
    )
    expect(within(workspace).queryByRole('link', { name: 'Back to run' })).not.toBeInTheDocument()

    const list = within(workspace).getByRole('list')
    expect(list).toHaveTextContent('Story Analysis started')
    expect(list).toHaveTextContent('Engine summary collected — passed')
    expect(
      within(workspace).getByText(/Derived from the latest durable run snapshot/),
    ).toBeInTheDocument()
  })
})
