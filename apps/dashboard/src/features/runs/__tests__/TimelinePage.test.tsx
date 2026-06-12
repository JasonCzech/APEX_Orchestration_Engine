import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PipelineStateSchema } from '@apex/pipeline-events'

import { deriveTimeline } from '@/features/runs/TimelinePage'
import { server } from '@/test/server'

import { PIPELINE_DETAIL, pipelineDetailHandler, renderRunRoutes, THREAD_ID } from './testUtils'

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
      labels.indexOf('Story Analysis succeeded'),
    )
    // Engine lifecycle markers from the execution entry.
    expect(labels.indexOf('Engine started (sim)')).toBeLessThan(
      labels.indexOf('Engine summary collected — passed'),
    )
  })

  it('renders the ledger rows with the honest-granularity caption', async () => {
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/timeline`])

    const list = await screen.findByRole('list')
    expect(list).toHaveTextContent('Story Analysis started')
    expect(list).toHaveTextContent('Engine summary collected — passed')
    expect(
      screen.getByText(/Derived from the latest state snapshot/),
    ).toBeInTheDocument()
  })
})
