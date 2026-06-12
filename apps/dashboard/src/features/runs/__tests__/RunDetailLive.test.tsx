import { cloneElement, isValidElement, type ReactNode } from 'react'

import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { server } from '@/test/server'

import type { LiveStreamViewLike } from '../liveTypes'

import { engineSamples, idleStreamView, streamView, toolCall } from './liveFixtures'
import {
  PIPELINE_DETAIL,
  PIPELINE_DETAIL_INTERRUPTED,
  pipelineDetailHandler,
  renderRunRoutes,
  THREAD_ID,
} from './testUtils'

/**
 * D2 live surfaces on RunDetailPage. The streaming module is mocked at the
 * integration-contract boundary (useRunLiveness -> {runId, stream}); the
 * scripted views come from liveFixtures (typed to liveTypes, never to the
 * streaming module's internals).
 */
const liveness: { current: { runId: string | null; stream: LiveStreamViewLike } } = vi.hoisted(
  () => ({ current: { runId: null, stream: { status: 'idle' } } }),
)

vi.mock('@/streaming/usePipelineStream', () => ({
  useRunLiveness: () => liveness.current,
}))

// recharts' ResponsiveContainer needs ResizeObserver/layout, absent in jsdom —
// pin the chart size (same convention as EngineStrip.test.tsx).
vi.mock('recharts', async (importOriginal) => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactNode }) =>
      isValidElement(children)
        ? cloneElement(children as React.ReactElement<{ width?: number; height?: number }>, {
            width: 600,
            height: 180,
          })
        : children,
  }
})

describe('RunDetailPage live surfaces (D2)', () => {
  it('shows the live status chip in the header', async () => {
    liveness.current = { runId: 'run-1', stream: streamView({ status: 'live' }) }
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=output`])

    const chip = await screen.findByTestId('live-status-chip')
    expect(chip).toHaveTextContent('live')
    expect(chip).toHaveClass('live')
  })

  it('defaults to the Activity tab while the thread is busy and renders the scripted feed', async () => {
    liveness.current = {
      runId: 'run-1',
      stream: streamView({
        status: 'live',
        phaseProgress: { execution: { status: 'running', attempt: 1 } },
        toolCalls: [toolCall('t1', 'engine.start', 'ok', 'execution')],
      }),
    }
    server.use(pipelineDetailHandler()) // thread_status: busy
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution`]) // no ?tab=

    const activityTab = await screen.findByRole('tab', { name: 'Activity' })
    expect(activityTab).toHaveAttribute('aria-selected', 'true')

    expect(await screen.findByTestId('activity-divider')).toHaveTextContent('running')
    const card = screen.getByTestId('activity-tool-card')
    expect(card).toHaveTextContent('engine.start')
  })

  it('keeps Output as the default tab when the thread is not busy', async () => {
    liveness.current = { runId: null, stream: idleStreamView() }
    server.use(pipelineDetailHandler({ ...PIPELINE_DETAIL, thread_status: 'idle' }))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/test_planning`])

    const outputTab = await screen.findByRole('tab', { name: 'Output' })
    expect(outputTab).toHaveAttribute('aria-selected', 'true')
  })

  it('renders the engine strip on the execution phase when poll samples stream in', async () => {
    const samples = engineSamples(12)
    liveness.current = {
      runId: 'run-1',
      stream: streamView({
        status: 'live',
        phaseProgress: { execution: { status: 'running', attempt: 1 } },
        engineStats: { samples, latest: samples[samples.length - 1] ?? null },
      }),
    }
    server.use(pipelineDetailHandler())
    renderRunRoutes([`/runs/${THREAD_ID}/phases/execution?tab=output`])

    const strip = await screen.findByTestId('engine-strip')
    expect(within(strip).getByTestId('engine-pill-vusers')).toHaveTextContent('50')
    expect(within(strip).getByTestId('engine-status')).toHaveTextContent('running')
  })

  it('surfaces pendingGateHint in the run rail until the snapshot delivers the interrupt', async () => {
    liveness.current = {
      runId: 'run-1',
      stream: streamView({
        status: 'live',
        pendingGateHint: { gate: 'prompt_review', phase: 'reporting' },
      }),
    }
    server.use(pipelineDetailHandler()) // no interrupts in the snapshot yet
    renderRunRoutes([`/runs/${THREAD_ID}/phases/reporting?tab=output`])

    const hint = await screen.findByTestId('gate-hint')
    expect(hint).toHaveTextContent('Gate opening: prompt_review on Reporting')
    expect(within(hint).getByText('loading gate…')).toHaveAttribute('title')
  })

  it('suppresses the hint once the snapshot hydrates the real interrupt', async () => {
    liveness.current = {
      runId: 'run-1',
      stream: streamView({
        status: 'live',
        pendingGateHint: { gate: 'phase_review', phase: 'reporting' },
      }),
    }
    server.use(pipelineDetailHandler(PIPELINE_DETAIL_INTERRUPTED))
    renderRunRoutes([`/runs/${THREAD_ID}/phases/reporting?tab=output`])

    // The hydrated gate banner wins; the hint chip is not duplicated.
    const banner = await screen.findByRole('status')
    expect(banner).toHaveTextContent('Gate open:')
    expect(screen.queryByTestId('gate-hint')).not.toBeInTheDocument()
  })
})
