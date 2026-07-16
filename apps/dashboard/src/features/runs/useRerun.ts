/**
 * useRerun — phase-subset re-run mutation on an EXISTING thread (plan Part 2
 * §4). The /v1 facade reloads the complete trusted checkpointed config and
 * changes only phase/gate selection. The browser never round-trips connection,
 * environment, prompt, or provider-affinity state.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import {
  getDurableIdempotencyKey,
  PIPELINE_RERUN_IDEMPOTENCY_STORAGE_KEY,
  retireDurableIdempotencyKey,
} from '@/utils/durableIdempotency'

/** Gates mode for the pre-flight modal's segmented control. */
export type GatesMode = 'inherit' | 'gated' | 'auto'

interface UniformGatePolicy {
  prompt_review: 'auto' | 'gated'
  output_review: 'auto' | 'gated'
}

/** Every phase fully gated (the backend default, stated explicitly). */
export const ALL_GATED_GATES: Record<PhaseName, UniformGatePolicy> = Object.fromEntries(
  PHASE_NAMES.map((phase) => [phase, { prompt_review: 'gated', output_review: 'gated' }]),
) as Record<PhaseName, UniformGatePolicy>

export interface RerunInput {
  threadId: string
  /** Canonical-order phase subset (configurable.phases). */
  phases: PhaseName[]
  gatesMode: GatesMode
}

export interface RerunResult {
  threadId: string
  runId: string
}

function createRerunIdempotencyKey(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `rerun-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

async function rerunPhases(
  { threadId, phases, gatesMode }: RerunInput,
): Promise<RerunResult> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const requestPayload = {
    thread_id: threadId,
    phases,
    gates_mode: gatesMode,
  }
  const idempotencyKey = await getDurableIdempotencyKey(
    PIPELINE_RERUN_IDEMPOTENCY_STORAGE_KEY,
    requestPayload,
    createRerunIdempotencyKey,
  )
  if (keyRevision !== getApiKeyRevision() || sessionRevision !== getSessionRevision()) {
    throw new Error('Credentials changed while preparing the rerun; please retry.')
  }
  const { data, error, response } = await getApexClient().POST(
    '/v1/pipelines/{thread_id}/rerun',
    {
      params: { path: { thread_id: threadId } },
      body: {
        phases,
        gates_mode: gatesMode,
        idempotency_key: idempotencyKey,
      },
    },
  )
  if (keyRevision !== getApiKeyRevision() || sessionRevision !== getSessionRevision()) {
    throw new Error('Credentials changed while rerunning the pipeline; retry to recover its result.')
  }
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Failed to rerun pipeline (${response.status})`),
      error,
    )
  }
  await retireDurableIdempotencyKey(
    PIPELINE_RERUN_IDEMPOTENCY_STORAGE_KEY,
    requestPayload,
    idempotencyKey,
  )
  return { threadId, runId: data.run_id }
}

/**
 * Mutation wrapper: 2xx invalidates the thread snapshot (new run/plan shows
 * up ahead of the 10s poll) and the pipelines lists (grid status flips to
 * busy). Navigation to /runs/{threadId}?tab=activity is the caller's job
 * (PreflightModal), mirroring useLaunchRun.
 */
export function useRerun() {
  const queryClient = useQueryClient()
  return useMutation<RerunResult, Error, RerunInput>({
    mutationFn: rerunPhases,
    onSuccess: (_data, variables) => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.threads.state(variables.threadId),
      })
      void queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.lists() })
    },
  })
}
