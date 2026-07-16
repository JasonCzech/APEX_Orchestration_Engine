import { useQuery } from '@tanstack/react-query'

import type { components } from '@apex/api-client'
import { PhaseResultEntrySchema, PipelineStateSchema, type PipelineState } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type GateInterrupt = components['schemas']['GateInterrupt']

export type PipelineDetail = components['schemas']['PipelineDetail']

/**
 * One snapshot read for the run-detail surfaces: the /v1/pipelines/{thread_id}
 * facade returns the dashboard summary, the full thread `values`, and the
 * pending interrupts in a single call (D1). The raw SDK `threads.get_state`
 * path is the D2 alternative once live streams patch this cache.
 */
export interface ThreadStateSnapshot {
  /** Facade response: summary fields + phase_strip + pending_gate. */
  detail: PipelineDetail
  /** `detail.values` parsed through the lenient PipelineState mirror. */
  state: PipelineState
  /** Pending HITL gates (empty when the thread is not interrupted). */
  interrupts: GateInterrupt[]
  /** True when the zod mirror rejected `values`; `state` is then the raw object. */
  stateParseFailed: boolean
}

/** Thread statuses that keep the fast snapshot poll alive. */
const ACTIVE_THREAD_STATUSES = new Set(['busy', 'interrupted'])

export const THREAD_STATE_REFETCH_MS = 10_000
/** Terminal threads still poll so externally started reruns become visible. */
export const TERMINAL_THREAD_STATE_REFETCH_MS = 30_000

export function threadStateRefetchInterval(status: string | null | undefined): number {
  return status && ACTIVE_THREAD_STATUSES.has(status)
    ? THREAD_STATE_REFETCH_MS
    : TERMINAL_THREAD_STATE_REFETCH_MS
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** Keep the run page usable when one server field drifts from the mirror schema. */
function normalizeDriftedState(raw: unknown): PipelineState {
  const source = isRecord(raw) ? raw : {}
  const safe: Record<string, unknown> = {}
  if (typeof source.title === 'string') safe.title = source.title
  if (typeof source.request === 'string') safe.request = source.request
  if (Array.isArray(source.phases_plan)) {
    safe.phases_plan = source.phases_plan.filter((phase): phase is string => typeof phase === 'string')
  }
  if (typeof source.current_phase === 'string' || source.current_phase === null) {
    safe.current_phase = source.current_phase
  }
  if (typeof source.run_aborted === 'boolean') safe.run_aborted = source.run_aborted
  if (isRecord(source.run_config)) safe.run_config = source.run_config
  if (isRecord(source.prompt_reviews)) safe.prompt_reviews = source.prompt_reviews
  if (isRecord(source.application_reviews)) safe.application_reviews = source.application_reviews
  if (isRecord(source.phase_results)) {
    safe.phase_results = Object.fromEntries(
      Object.entries(source.phase_results)
        .filter(([, entry]) => isRecord(entry))
        .map(([phase, entry]) => {
          const candidate = entry as Record<string, unknown>
          const parsed = PhaseResultEntrySchema.safeParse(candidate)
          if (parsed.success) return [phase, parsed.data]
          // Preserve every valid field independently. A new enum value in one
          // field must not erase test summaries, artifacts, approvals, tool
          // calls, prompt provenance, or engine telemetry from the same entry.
          const normalized: Record<string, unknown> = { ...candidate }
          for (const [field, schema] of Object.entries(PhaseResultEntrySchema.shape)) {
            const fieldResult = schema.safeParse(candidate[field])
            if (fieldResult.success) normalized[field] = fieldResult.data
            else delete normalized[field]
          }
          return [phase, normalized]
        }),
    )
  }
  if (Array.isArray(source.artifacts)) safe.artifacts = source.artifacts.filter(isRecord)
  if (Array.isArray(source.dialogue)) safe.dialogue = source.dialogue.filter(isRecord)
  if (Array.isArray(source.context_packets)) safe.context_packets = source.context_packets.filter(isRecord)
  if (isRecord(source.engine_handle)) safe.engine_handle = source.engine_handle
  return safe as PipelineState
}

async function fetchThreadState(threadId: string): Promise<ThreadStateSnapshot> {
  const { data, error, response } = await getApexClient().GET('/v1/pipelines/{thread_id}', {
    params: { path: { thread_id: threadId } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Failed to load run ${threadId} (${response.status})`),
      error,
    )
  }
  const rawValues = data.values ?? {}
  const parsed = PipelineStateSchema.safeParse(rawValues)
  if (!parsed.success) {
    // Mirror policy: state schemas never blank the page — log and render raw.
    console.warn('[useThreadState] PipelineState mirror rejected thread values', {
      issueCount: parsed.error.issues.length,
    })
  }
  return {
    detail: data,
    state: parsed.success ? parsed.data : normalizeDriftedState(rawValues),
    interrupts: data.interrupts ?? [],
    stateParseFailed: !parsed.success,
  }
}

/**
 * Snapshot of a pipeline thread (summary + values + interrupts).
 * staleTime 0 (plan: thread state is always refetched on mount); polls every
 * 10s while busy/interrupted and every 30s while terminal so an open page
 * notices a rerun launched by another operator.
 */
export function useThreadState(threadId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.threads.state(threadId ?? ''),
    queryFn: () => fetchThreadState(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: STALE_TIMES.threadState,
    refetchInterval: (query) => {
      const status = query.state.data?.detail.thread_status
      return threadStateRefetchInterval(status)
    },
    refetchIntervalInBackground: false,
  })
}
