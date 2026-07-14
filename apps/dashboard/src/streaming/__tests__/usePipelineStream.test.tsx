/**
 * usePipelineStream behavior against the scriptable fake LangGraph client:
 * happy-path projections + cache patches, engine_poll flood coalescing (zero
 * cache writes), reconnect with lastEventId resume, resume-window fallback,
 * gate hints, single healing invalidate at stream end, abort on unmount.
 */
import type { ReactNode } from 'react'

import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { PHASE_NAMES, type PhaseName, type PhaseStatus } from '@apex/pipeline-events'

import type { PipelineListResponse, PhaseStripEntry } from '@/api/hooks/usePipelines'
import type { ThreadStateSnapshot } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'

import { resumeStore } from '../resumeStore'
import { ENGINE_SAMPLE_CAP, usePipelineStream } from '../usePipelineStream'
import { FakeLangGraphClient } from './fakeLangGraph'

const clientHolder = vi.hoisted(() => ({ current: null as unknown }))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () => Promise.resolve(clientHolder.current),
  resetLangGraphClient: () => {},
}))

let fake: FakeLangGraphClient

beforeEach(() => {
  vi.useFakeTimers()
  window.sessionStorage.clear()
  fake = new FakeLangGraphClient()
  clientHolder.current = fake.asClient()
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

// ---------- fixtures ----------

function makeStrip(): PhaseStripEntry[] {
  return PHASE_NAMES.map((phase) => ({ phase, status: 'pending', attempt: null }))
}

function makeSnapshot(threadId = 't1'): ThreadStateSnapshot {
  return {
    detail: {
      thread_id: threadId,
      title: `Run ${threadId}`,
      thread_status: 'busy',
      current_phase: null,
      phase_strip: makeStrip(),
      pending_gate: null,
    },
    state: { phase_results: {} },
    interrupts: [],
    stateParseFailed: false,
  }
}

function makeListResponse(threadId = 't1'): PipelineListResponse {
  return {
    items: [
      {
        thread_id: threadId,
        title: `Run ${threadId}`,
        thread_status: 'busy',
        current_phase: null,
        phase_strip: makeStrip(),
        pending_gate: null,
      },
    ],
    limit: 25,
    offset: 0,
  }
}

const planResolved = { schema_version: 1, type: 'plan_resolved', phases: [...PHASE_NAMES] }

function phaseStatus(phase: PhaseName, status: PhaseStatus, attempt = 1) {
  return { schema_version: 1, type: 'phase_status', phase, status, attempt }
}

function toolCall(id: string, phase: PhaseName = 'story_analysis') {
  return { schema_version: 1, type: 'tool_call', phase, id, tool: 'jira_search', status: 'ok' }
}

function agentMessage(phase: PhaseName = 'story_analysis') {
  return {
    schema_version: 1,
    type: 'agent_message',
    phase,
    model: 'claude-sonnet-4-5',
    chars: 842,
  }
}

function agentError(phase: PhaseName = 'story_analysis') {
  return {
    schema_version: 1,
    type: 'agent_error',
    phase,
    error: 'provider request timed out',
  }
}

function enginePollError() {
  return {
    schema_version: 1,
    type: 'engine_poll_error',
    phase: 'execution',
    attempt: 1,
    error: 'provider status request timed out',
    consecutive_errors: 2,
  }
}

function gateOpened(gate: 'prompt_review' | 'phase_review', phase: PhaseName, attempt = 1) {
  return { schema_version: 1, type: 'gate_opened', gate, phase, attempt }
}

function enginePoll(progress: number) {
  return {
    schema_version: 1,
    type: 'engine_poll',
    phase: 'execution',
    attempt: 1,
    engine: 'sim',
    external_run_id: 'sim-77',
    status: 'running',
    progress_pct: progress,
    live_stats: { vusers: 25, tps: 110.5, error_rate: 0.2, p95_ms: 480 },
  }
}

// ---------- harness ----------

function setup(seed = true) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } },
  })
  if (seed) {
    queryClient.setQueryData(queryKeys.threads.state('t1'), makeSnapshot())
    queryClient.setQueryData(queryKeys.pipelines.list({}), makeListResponse())
  }
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  )
  return { queryClient, wrapper }
}

/** Flush microtasks + due 0ms timers so the stream loop processes parts. */
async function pump(ms = 0): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

function snapshotOf(queryClient: QueryClient): ThreadStateSnapshot {
  const data = queryClient.getQueryData<ThreadStateSnapshot>(queryKeys.threads.state('t1'))
  if (!data) throw new Error('snapshot missing from cache')
  return data
}

function listRowOf(queryClient: QueryClient) {
  const data = queryClient.getQueryData<PipelineListResponse>(queryKeys.pipelines.list({}))
  const row = data?.items[0]
  if (!row) throw new Error('list row missing from cache')
  return row
}

describe('usePipelineStream', () => {
  it('stays idle without a runId and never opens a stream', async () => {
    const { wrapper } = setup()
    const { result } = renderHook(() => usePipelineStream('t1', null), { wrapper })
    await pump()
    expect(result.current.status).toBe('idle')
    expect(fake.joinStreamCalls).toHaveLength(0)
  })

  it('happy path: events drive phaseProgress/toolCalls and patch both caches', async () => {
    const { queryClient, wrapper } = setup()
    const stream = fake.scriptStream()
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()
    expect(result.current.status).toBe('connecting')
    expect(fake.joinStreamCalls).toHaveLength(1)
    expect(fake.joinStreamCalls[0]?.options?.streamMode).toBe('custom')
    expect(fake.joinStreamCalls[0]?.options?.lastEventId).toBeUndefined()

    await act(async () => {
      stream.pushCustom(planResolved, 'ev-1')
      stream.pushCustom(phaseStatus('story_analysis', 'running'), 'ev-2')
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('live')
    expect(result.current.plan).toEqual([...PHASE_NAMES])
    expect(result.current.phaseProgress.story_analysis).toEqual({ status: 'running', attempt: 1 })
    expect(result.current.phaseProgress.execution).toEqual({ status: 'pending', attempt: 0 })

    await act(async () => {
      stream.pushCustom(toolCall('tc-1'), 'ev-3')
      stream.pushCustom(agentMessage(), 'ev-3a')
      stream.pushCustom(agentError(), 'ev-3b')
      stream.pushCustom(enginePollError(), 'ev-3c')
      stream.pushCustom(phaseStatus('story_analysis', 'succeeded'), 'ev-4')
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.toolCalls).toHaveLength(1)
    expect(result.current.toolCalls[0]?.id).toBe('tc-1')
    expect(result.current.agentEvents).toEqual([
      expect.objectContaining({ type: 'agent_message', model: 'claude-sonnet-4-5' }),
      expect.objectContaining({ type: 'agent_error', error: 'provider request timed out' }),
    ])
    expect(result.current.engineErrors).toEqual([
      expect.objectContaining({
        type: 'engine_poll_error',
        error: 'provider status request timed out',
        consecutive_errors: 2,
      }),
    ])
    expect(result.current.driftCount).toBe(0)
    expect(result.current.phaseProgress.story_analysis).toEqual({
      status: 'succeeded',
      attempt: 1,
    })

    // threads.state snapshot patched (phase_results + phase_strip + plan)
    const snapshot = snapshotOf(queryClient)
    expect(snapshot.state.phases_plan).toEqual([...PHASE_NAMES])
    expect(snapshot.state.phase_results?.story_analysis).toMatchObject({
      phase: 'story_analysis',
      status: 'succeeded',
      attempt: 1,
    })
    expect(
      snapshot.detail.phase_strip.find((entry) => entry.phase === 'story_analysis'),
    ).toMatchObject({ status: 'succeeded', attempt: 1 })

    // pipelines.list row strip patched too
    expect(listRowOf(queryClient).phase_strip.find((s) => s.phase === 'story_analysis')).toMatchObject(
      { status: 'succeeded', attempt: 1 },
    )

    // resume cursor tracked from part ids
    expect(resumeStore.get('t1', 'r1')).toBe('ev-4')
  })

  it('attempt-aware merge: a new attempt replaces the cached phase entry', async () => {
    const { queryClient, wrapper } = setup()
    const seeded = makeSnapshot()
    seeded.state = {
      phase_results: {
        story_analysis: {
          phase: 'story_analysis',
          status: 'failed',
          attempt: 1,
          summary: 'stale attempt-1 summary',
        },
      },
    }
    queryClient.setQueryData<ThreadStateSnapshot>(queryKeys.threads.state('t1'), seeded)
    const stream = fake.scriptStream()
    renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()

    // Same attempt: merge keeps existing fields.
    await act(async () => {
      stream.pushCustom(phaseStatus('story_analysis', 'running', 1))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(snapshotOf(queryClient).state.phase_results?.story_analysis).toMatchObject({
      status: 'running',
      attempt: 1,
      summary: 'stale attempt-1 summary',
    })

    // New attempt: replace, dropping attempt-1 fields.
    await act(async () => {
      stream.pushCustom(phaseStatus('story_analysis', 'running', 2))
      await vi.advanceTimersByTimeAsync(0)
    })
    const entry = snapshotOf(queryClient).state.phase_results?.story_analysis
    expect(entry).toMatchObject({ status: 'running', attempt: 2 })
    expect(entry?.summary).toBeUndefined()
  })

  it('engine_poll flood: coalesced flushes (50ms floor) and ZERO query-cache writes', async () => {
    const { queryClient, wrapper } = setup()
    const stream = fake.scriptStream()
    const engineRefs = new Set<unknown>()
    const { result } = renderHook(
      () => {
        const view = usePipelineStream('t1', 'r1')
        engineRefs.add(view.engineStats)
        return view
      },
      { wrapper },
    )
    await pump()

    const setQueryData = vi.spyOn(queryClient, 'setQueryData')
    const setQueriesData = vi.spyOn(queryClient, 'setQueriesData')

    // 500 events across 1000ms of fake time (10 bursts of 50 every 100ms).
    const durationMs = 1_000
    for (let burst = 0; burst < 10; burst += 1) {
      await act(async () => {
        for (let i = 0; i < 50; i += 1) {
          stream.pushCustom(enginePoll(burst * 50 + i))
        }
        await vi.advanceTimersByTimeAsync(durationMs / 10)
      })
    }

    // Ring respects the 300 cap; latest reflects the final event.
    expect(result.current.engineStats.samples).toHaveLength(ENGINE_SAMPLE_CAP)
    expect(result.current.engineStats.latest?.progress_pct).toBe(499)
    expect(result.current.engineStats.samples.at(-1)?.progress_pct).toBe(499)
    expect(result.current.engineStats.samples[0]?.progress_pct).toBe(200)

    // <= ceil(duration / 50ms) flushes (initial ref excluded), and at least one.
    const flushes = engineRefs.size - 1
    expect(flushes).toBeGreaterThan(0)
    expect(flushes).toBeLessThanOrEqual(Math.ceil(durationMs / 50))

    // The strict rule: engine_poll NEVER touches the react-query cache.
    expect(setQueryData).not.toHaveBeenCalled()
    expect(setQueriesData).not.toHaveBeenCalled()
  })

  it('disconnect mid-stream: reconnecting status, then rejoin with stored lastEventId', async () => {
    const { wrapper } = setup()
    const first = fake.scriptStream()
    const second = fake.scriptStream()
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()

    await act(async () => {
      first.pushCustom(phaseStatus('story_analysis', 'running'), 'ev-5')
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('live')

    await act(async () => {
      first.fail(new TypeError('network dropped'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('reconnecting')
    expect(result.current.error?.message).toContain('network dropped')
    expect(fake.joinStreamCalls).toHaveLength(1)

    // Backoff attempt 1 is 1000..1250ms jittered.
    await pump(1_500)
    expect(fake.joinStreamCalls).toHaveLength(2)
    expect(fake.joinStreamCalls[1]?.options?.lastEventId).toBe('ev-5')

    await act(async () => {
      second.pushCustom(phaseStatus('story_analysis', 'succeeded'), 'ev-6')
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('live')
    expect(result.current.phaseProgress.story_analysis?.status).toBe('succeeded')
  })

  it('resume-window failure: clears the cursor, refetches the snapshot, rejoins live', async () => {
    const { queryClient, wrapper } = setup()
    resumeStore.set('t1', 'r1', 'ev-99')
    const first = fake.scriptStream()
    const second = fake.scriptStream()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()
    expect(fake.joinStreamCalls[0]?.options?.lastEventId).toBe('ev-99')

    await act(async () => {
      first.fail(new Error('410: resume window expired'))
      await vi.advanceTimersByTimeAsync(0)
    })
    // Cursor dropped + snapshot healing refetch queued.
    expect(resumeStore.get('t1', 'r1')).toBeNull()
    expect(
      invalidate.mock.calls.filter(
        ([filters]) =>
          JSON.stringify(filters?.queryKey) === JSON.stringify(queryKeys.threads.state('t1')),
      ),
    ).toHaveLength(1)
    expect(result.current.status).toBe('reconnecting')

    await pump(1_500)
    expect(fake.joinStreamCalls).toHaveLength(2)
    expect(fake.joinStreamCalls[1]?.options?.lastEventId).toBeUndefined()

    await act(async () => {
      second.pushCustom(phaseStatus('story_analysis', 'running'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('live')
  })

  it('preserves the newest cursor when a resumed stream later disconnects', async () => {
    const { queryClient, wrapper } = setup()
    resumeStore.set('t1', 'r1', 'ev-old')
    const first = fake.scriptStream()
    fake.scriptStream()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()
    expect(fake.joinStreamCalls[0]?.options?.lastEventId).toBe('ev-old')

    await act(async () => {
      first.pushCustom(phaseStatus('story_analysis', 'running'), 'ev-new')
      await vi.advanceTimersByTimeAsync(0)
      first.fail(new TypeError('network dropped'))
      await vi.advanceTimersByTimeAsync(0)
    })

    expect(resumeStore.get('t1', 'r1')).toBe('ev-new')
    expect(invalidate).not.toHaveBeenCalled()
    await pump(1_500)
    expect(fake.joinStreamCalls[1]?.options?.lastEventId).toBe('ev-new')
  })

  it('gate_opened sets the pending hint and patches cached rows', async () => {
    const { queryClient, wrapper } = setup()
    const stream = fake.scriptStream()
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()

    await act(async () => {
      stream.pushCustom(gateOpened('prompt_review', 'test_planning'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.pendingGateHint).toEqual({
      gate: 'prompt_review',
      phase: 'test_planning',
    })
    expect(result.current.phaseProgress.test_planning?.status).toBe('awaiting_prompt_review')

    const expectedGate = { interrupt_id: null, kind: 'prompt_review', phase: 'test_planning' }
    expect(snapshotOf(queryClient).detail.pending_gate).toEqual(expectedGate)
    expect(listRowOf(queryClient).pending_gate).toEqual(expectedGate)

    // The phase moving on clears the hint.
    await act(async () => {
      stream.pushCustom(phaseStatus('test_planning', 'running'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.pendingGateHint).toBeNull()
  })

  it('stream end while busy: reconnects and preserves the resume cursor', async () => {
    const { queryClient, wrapper } = setup()
    const stream = fake.scriptStream()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()

    await act(async () => {
      stream.pushCustom(phaseStatus('story_analysis', 'running'), 'ev-1')
      stream.pushCustom(phaseStatus('story_analysis', 'succeeded'), 'ev-2')
      stream.end()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('reconnecting')
    expect(resumeStore.get('t1', 'r1')).toBe('ev-2')
    const healingCalls = invalidate.mock.calls.filter(
      ([filters]) =>
        JSON.stringify(filters?.queryKey) === JSON.stringify(queryKeys.threads.state('t1')),
    )
    expect(healingCalls).toHaveLength(1)

    await pump(1_500)
    expect(fake.joinStreamCalls).toHaveLength(2)
    expect(fake.joinStreamCalls[1]?.options?.lastEventId).toBe('ev-2')
  })

  it('terminal error event surfaces as status error (single healing refetch still applies)', async () => {
    const { queryClient, wrapper } = setup()
    const stream = fake.scriptStream()
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()

    await act(async () => {
      stream.push({ event: 'error', data: 'ValueError: engine exploded' })
      stream.end()
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.status).toBe('error')
    expect(result.current.error?.message).toContain('engine exploded')
    expect(
      invalidate.mock.calls.filter(
        ([filters]) =>
          JSON.stringify(filters?.queryKey) === JSON.stringify(queryKeys.threads.state('t1')),
      ),
    ).toHaveLength(1)
  })

  it('schema drift: unknown event types bump driftCount and warn instead of crashing', async () => {
    const { wrapper } = setup()
    const stream = fake.scriptStream()
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const { result } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()

    await act(async () => {
      stream.pushCustom({ schema_version: 1, type: 'totally_new_event', payload: 1 })
      stream.pushCustom(phaseStatus('story_analysis', 'running'))
      await vi.advanceTimersByTimeAsync(0)
    })
    expect(result.current.driftCount).toBe(1)
    expect(result.current.status).toBe('live')
    expect(result.current.phaseProgress.story_analysis?.status).toBe('running')
    expect(warn).toHaveBeenCalled()
  })

  it('abort on unmount: cancels the SSE signal and never reconnects', async () => {
    const { wrapper } = setup()
    const stream = fake.scriptStream()
    const { unmount } = renderHook(() => usePipelineStream('t1', 'r1'), { wrapper })
    await pump()
    expect(fake.joinStreamCalls).toHaveLength(1)
    const call = fake.joinStreamCalls[0]

    unmount()
    expect(call?.options?.signal?.aborted).toBe(true)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000)
    })
    expect(stream.sawAbort).toBe(true)
    expect(fake.joinStreamCalls).toHaveLength(1)
  })
})
