/**
 * Snapshot + tail reconciliation (plan Part 2 — Data layer): low-frequency
 * stream events patch the cached `threads.state` snapshot (canonical) and the
 * `pipelines.list` rows so every mounted surface moves together.
 *
 * Strict rule: telemetry-only events (tool/agent/engine poll/error) NEVER
 * write the query cache — they stay in the stream view / ring buffers.
 * Monotonicity stays simple per plan:
 * patches are merges, and the one healing refetch at stream end/error wins.
 */
import type { QueryClient } from '@tanstack/react-query'

import type {
  GateOpenedEvent,
  PhaseStatusEvent,
  PipelineEvent,
  PlanResolvedEvent,
} from '@apex/pipeline-events'

import type {
  PendingGate,
  PhaseStripEntry,
  PipelineListResponse,
  PipelineSummary,
} from '@/api/hooks/usePipelines'
import type { ThreadStateSnapshot } from '@/api/hooks/useThreadState'
import { queryKeys } from '@/api/queryKeys'

export function applyStreamEvent(
  queryClient: QueryClient,
  threadId: string,
  event: PipelineEvent,
): void {
  switch (event.type) {
    case 'plan_resolved':
      applyPlanResolved(queryClient, threadId, event)
      return
    case 'phase_status':
      applyPhaseStatus(queryClient, threadId, event)
      return
    case 'gate_opened':
      applyGateOpened(queryClient, threadId, event)
      return
    default:
      // Telemetry-only events: live feeds, never cache writes.
      return
  }
}

function applyPlanResolved(
  queryClient: QueryClient,
  threadId: string,
  event: PlanResolvedEvent,
): void {
  queryClient.setQueryData<ThreadStateSnapshot>(queryKeys.threads.state(threadId), (prev) => {
    if (!prev) return prev
    return { ...prev, state: { ...prev.state, phases_plan: event.phases } }
  })
}

function applyPhaseStatus(
  queryClient: QueryClient,
  threadId: string,
  event: PhaseStatusEvent,
): void {
  const currentPhasePatch =
    event.status === 'running'
      ? { current_phase: event.phase }
      : ['succeeded', 'failed', 'skipped', 'aborted'].includes(event.status)
        ? { current_phase: null }
        : {}
  queryClient.setQueryData<ThreadStateSnapshot>(queryKeys.threads.state(threadId), (prev) => {
    if (!prev) return prev
    const results = prev.state.phase_results ?? {}
    const existing = results[event.phase]
    if (typeof existing?.attempt === 'number' && existing.attempt > event.attempt) return prev
    // Attempt-aware merge mirroring the backend phase_results reducer:
    // same attempt merges onto the entry, a new attempt replaces it.
    const entry =
      existing && existing.attempt === event.attempt
        ? { ...existing, status: event.status }
        : { phase: event.phase, status: event.status, attempt: event.attempt }
    return {
      ...prev,
      detail: {
        ...prev.detail,
        phase_strip: patchStrip(prev.detail.phase_strip, event),
        ...currentPhasePatch,
      },
      state: {
        ...prev.state,
        phase_results: { ...results, [event.phase]: entry },
      },
    }
  })
  patchListRows(queryClient, threadId, (row) => ({
    ...(isOlderStripEvent(row.phase_strip, event)
      ? row
      : {
          ...row,
          phase_strip: patchStrip(row.phase_strip, event),
          ...currentPhasePatch,
        }),
  }))
}

function isOlderStripEvent(
  strip: PhaseStripEntry[] | undefined,
  event: Pick<PhaseStatusEvent, 'phase' | 'attempt'>,
): boolean {
  const current = strip?.find((segment) => segment.phase === event.phase)
  return typeof current?.attempt === 'number' && current.attempt > event.attempt
}

function applyGateOpened(
  queryClient: QueryClient,
  threadId: string,
  event: GateOpenedEvent,
): void {
  // interrupt_id is not on the wire event; D3's inbox hydrates it on focus.
  const pendingGate: PendingGate = { interrupt_id: null, kind: event.gate, phase: event.phase }
  queryClient.setQueryData<ThreadStateSnapshot>(queryKeys.threads.state(threadId), (prev) => {
    if (!prev) return prev
    const existing = prev.state.phase_results?.[event.phase]
    if (
      (typeof existing?.attempt === 'number' && existing.attempt > event.attempt) ||
      isOlderStripEvent(prev.detail.phase_strip, event)
    ) return prev
    return { ...prev, detail: { ...prev.detail, pending_gate: pendingGate } }
  })
  patchListRows(queryClient, threadId, (row) =>
    isOlderStripEvent(row.phase_strip, event) ? row : { ...row, pending_gate: pendingGate },
  )
}

function patchStrip(
  strip: PhaseStripEntry[] | undefined,
  event: PhaseStatusEvent,
): PhaseStripEntry[] {
  if (!strip) return []
  return strip.map((segment) =>
    segment.phase === event.phase
      ? typeof segment.attempt === 'number' && segment.attempt > event.attempt
        ? segment
        : { ...segment, status: event.status, attempt: event.attempt }
      : segment,
  )
}

function patchListRows(
  queryClient: QueryClient,
  threadId: string,
  patch: (row: PipelineSummary) => PipelineSummary,
): void {
  queryClient.setQueriesData<PipelineListResponse>(
    { queryKey: queryKeys.pipelines.lists() },
    (prev) => {
      if (!prev) return prev
      let touched = false
      const items = prev.items.map((row) => {
        if (row.thread_id !== threadId) return row
        touched = true
        return patch(row)
      })
      return touched ? { ...prev, items } : prev
    },
  )
}
