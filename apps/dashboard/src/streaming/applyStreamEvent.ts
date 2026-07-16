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
  PhaseName,
  PhaseStatus,
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

type PhaseTransition = Pick<PhaseStatusEvent, 'phase' | 'status' | 'attempt'>

const TERMINAL_PHASE_STATUSES: ReadonlySet<PhaseStatus> = new Set([
  'succeeded',
  'failed',
  'skipped',
  'aborted',
])

function isTerminalPhaseStatus(status: unknown): status is PhaseStatus {
  return typeof status === 'string' && TERMINAL_PHASE_STATUSES.has(status as PhaseStatus)
}

function isAwaitingPhaseStatus(status: PhaseStatus): boolean {
  return status === 'awaiting_prompt_review' || status === 'awaiting_output_review'
}

function rejectsPhaseTransition(
  current: { status?: unknown; attempt?: number | null } | undefined,
  event: PhaseTransition,
): boolean {
  if (typeof current?.attempt !== 'number') return false
  if (current.attempt > event.attempt) return true
  return (
    current.attempt === event.attempt &&
    isTerminalPhaseStatus(current.status) &&
    current.status !== event.status
  )
}

function rejectsGateTransition(
  current: { status?: unknown; attempt?: number | null } | undefined,
  event: PhaseTransition,
): boolean {
  if (rejectsPhaseTransition(current, event)) return true
  return (
    current?.attempt === event.attempt &&
    current.status === 'awaiting_output_review' &&
    event.status === 'awaiting_prompt_review'
  )
}

function nextCurrentPhase(
  currentPhase: PhaseName | null | undefined,
  event: PhaseTransition,
  restartedAttempt: boolean,
): PhaseName | null | undefined
function nextCurrentPhase(
  currentPhase: string | null | undefined,
  event: PhaseTransition,
  restartedAttempt: boolean,
): string | null | undefined
function nextCurrentPhase(
  currentPhase: string | null | undefined,
  event: PhaseTransition,
  restartedAttempt: boolean,
): string | null | undefined {
  if (event.status === 'running') {
    // A late same-attempt event for an earlier phase must not move the snapshot
    // backwards. A true retry has a higher attempt and may intentionally do so.
    return currentPhase === null || currentPhase === undefined || currentPhase === event.phase || restartedAttempt
      ? event.phase
      : currentPhase
  }
  if (isTerminalPhaseStatus(event.status) && currentPhase === event.phase) return null
  return currentPhase
}

function nextPendingGate(
  pendingGate: PendingGate | null | undefined,
  event: PhaseTransition,
): PendingGate | null | undefined {
  return pendingGate?.phase === event.phase && !isAwaitingPhaseStatus(event.status)
    ? null
    : pendingGate
}

export function applyStreamEvent(
  queryClient: QueryClient,
  threadId: string,
  event: PipelineEvent,
): boolean {
  switch (event.type) {
    case 'plan_resolved':
      applyPlanResolved(queryClient, threadId, event)
      return true
    case 'phase_status':
      return applyPhaseStatus(queryClient, threadId, event)
    case 'gate_opened':
      return applyGateOpened(queryClient, threadId, event)
    default:
      // Telemetry-only events: live feeds, never cache writes.
      return true
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
): boolean {
  let accepted = true
  queryClient.setQueryData<ThreadStateSnapshot>(queryKeys.threads.state(threadId), (prev) => {
    if (!prev) return prev
    const results = prev.state.phase_results ?? {}
    const existing = results[event.phase]
    const stripEntry = prev.detail.phase_strip?.find((segment) => segment.phase === event.phase)
    if (rejectsPhaseTransition(existing, event) || rejectsPhaseTransition(stripEntry, event)) {
      accepted = false
      return prev
    }
    // Attempt-aware merge mirroring the backend phase_results reducer:
    // same attempt merges onto the entry, a new attempt replaces it.
    const restartedAttempt =
      typeof existing?.attempt === 'number' && existing.attempt < event.attempt
    const entry =
      existing && existing.attempt === event.attempt
        ? { ...existing, status: event.status }
        : { phase: event.phase, status: event.status, attempt: event.attempt }
    const currentPhase = nextCurrentPhase(prev.detail.current_phase, event, restartedAttempt)
    return {
      ...prev,
      detail: {
        ...prev.detail,
        phase_strip: patchStrip(prev.detail.phase_strip, event),
        current_phase: currentPhase ?? null,
        pending_gate: nextPendingGate(prev.detail.pending_gate, event) ?? null,
      },
      state: {
        ...prev.state,
        current_phase: nextCurrentPhase(prev.state.current_phase, event, restartedAttempt),
        phase_results: { ...results, [event.phase]: entry },
      },
    }
  })
  // The snapshot is the canonical baseline for a resumed/replayed stream. Do
  // not let the list cache or live reducer regress after it rejected an event.
  if (!accepted) return false
  patchListRows(queryClient, threadId, (row) => {
    const existing = row.phase_strip?.find((segment) => segment.phase === event.phase)
    if (rejectsPhaseTransition(existing, event)) return row
    const restartedAttempt =
      typeof existing?.attempt === 'number' && existing.attempt < event.attempt
    return {
      ...row,
      phase_strip: patchStrip(row.phase_strip, event),
      current_phase: nextCurrentPhase(row.current_phase, event, restartedAttempt) ?? null,
      pending_gate: nextPendingGate(row.pending_gate, event) ?? null,
    }
  })
  return true
}

function rejectsStripTransition(
  strip: PhaseStripEntry[] | undefined,
  event: PhaseTransition,
  gate = false,
): boolean {
  const current = strip?.find((segment) => segment.phase === event.phase)
  return gate ? rejectsGateTransition(current, event) : rejectsPhaseTransition(current, event)
}

function applyGateOpened(
  queryClient: QueryClient,
  threadId: string,
  event: GateOpenedEvent,
): boolean {
  // interrupt_id is not on the wire event; D3's inbox hydrates it on focus.
  const pendingGate: PendingGate = { interrupt_id: null, kind: event.gate, phase: event.phase }
  const phaseEvent: PhaseTransition = {
    phase: event.phase,
    status: event.gate === 'prompt_review' ? 'awaiting_prompt_review' : 'awaiting_output_review',
    attempt: event.attempt,
  }
  let accepted = true
  queryClient.setQueryData<ThreadStateSnapshot>(queryKeys.threads.state(threadId), (prev) => {
    if (!prev) return prev
    const results = prev.state.phase_results ?? {}
    const existing = results[event.phase]
    if (
      rejectsGateTransition(existing, phaseEvent) ||
      rejectsStripTransition(prev.detail.phase_strip, phaseEvent, true)
    ) {
      accepted = false
      return prev
    }
    const restartedAttempt =
      typeof existing?.attempt === 'number' && existing.attempt < event.attempt
    const entry =
      existing && existing.attempt === event.attempt
        ? { ...existing, status: phaseEvent.status }
        : { phase: event.phase, status: phaseEvent.status, attempt: event.attempt }
    const currentPhase =
      prev.detail.current_phase === null ||
      prev.detail.current_phase === undefined ||
      prev.detail.current_phase === event.phase ||
      restartedAttempt
        ? event.phase
        : prev.detail.current_phase
    return {
      ...prev,
      detail: {
        ...prev.detail,
        current_phase: currentPhase,
        pending_gate: pendingGate,
        phase_strip: patchStrip(prev.detail.phase_strip, phaseEvent),
      },
      state: {
        ...prev.state,
        current_phase:
          prev.state.current_phase === null ||
          prev.state.current_phase === undefined ||
          prev.state.current_phase === event.phase ||
          restartedAttempt
            ? event.phase
            : prev.state.current_phase,
        phase_results: { ...results, [event.phase]: entry },
      },
    }
  })
  if (!accepted) return false
  patchListRows(queryClient, threadId, (row) => {
    const existing = row.phase_strip?.find((segment) => segment.phase === event.phase)
    if (rejectsGateTransition(existing, phaseEvent)) return row
    const restartedAttempt =
      typeof existing?.attempt === 'number' && existing.attempt < event.attempt
    const currentPhase =
      row.current_phase === null ||
      row.current_phase === event.phase ||
      restartedAttempt
        ? event.phase
        : row.current_phase
    return {
      ...row,
      current_phase: currentPhase,
      pending_gate: pendingGate,
      phase_strip: patchStrip(row.phase_strip, phaseEvent),
    }
  })
  return true
}

function patchStrip(
  strip: PhaseStripEntry[] | undefined,
  event: PhaseTransition,
): PhaseStripEntry[] {
  if (!strip) return []
  return strip.map((segment) =>
    segment.phase === event.phase
      ? rejectsPhaseTransition(segment, event)
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
