import { useQuery } from '@tanstack/react-query'

import type { components } from '@apex/api-client'
import { PipelineStateSchema, type PipelineState } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type GateInterrupt = components['schemas']['GateInterrupt']

/**
 * Local extension: the generated @apex/api-client schema predates the `engine`
 * field on PipelineDetail (present in docs/api/apex-v1.openapi.json and the
 * live backend — same drift the runs-grid ROUTES.md documents). Delete the
 * extension once the client is regenerated.
 */
export type PipelineDetail = components['schemas']['PipelineDetail'] & {
  engine?: { engine?: string | null; external_run_id?: string | null } | null
}

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

/** Thread statuses that keep the 10s snapshot poll alive. */
const ACTIVE_THREAD_STATUSES = new Set(['busy', 'interrupted'])

export const THREAD_STATE_REFETCH_MS = 10_000

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
      threadId,
      issues: parsed.error.issues,
    })
  }
  return {
    detail: data,
    state: parsed.success ? parsed.data : (rawValues as PipelineState),
    interrupts: data.interrupts ?? [],
    stateParseFailed: !parsed.success,
  }
}

/**
 * Snapshot of a pipeline thread (summary + values + interrupts).
 * staleTime 0 (plan: thread state is always refetched on mount); polls every
 * 10s while the thread is busy/interrupted, off otherwise.
 */
export function useThreadState(threadId: string | undefined) {
  return useQuery({
    queryKey: queryKeys.threads.state(threadId ?? ''),
    queryFn: () => fetchThreadState(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: STALE_TIMES.threadState,
    refetchInterval: (query) => {
      const status = query.state.data?.detail.thread_status
      return status && ACTIVE_THREAD_STATUSES.has(status) ? THREAD_STATE_REFETCH_MS : false
    },
  })
}
