import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { PipelineStateSchema } from '@apex/pipeline-events'

import { deriveTimeline } from '@/features/runs/timelineModel'
import { server } from '@/test/server'

import { PIPELINE_DETAIL, pipelineDetailHandler, renderRunRoutes, THREAD_ID } from './testUtils'

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
