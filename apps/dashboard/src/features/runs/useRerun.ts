/**
 * useRerun — phase-subset re-run mutation on an EXISTING thread (plan Part 2
 * §4). Extends the D2 launch path (launchRun.ts): same SDK client, same
 * stream/durability options, but no thread create — runs.create targets the
 * existing thread so plan_resolver resolves prerequisites against its
 * succeeded phase_results.
 *
 * input is {} and NOT null: null means "continue from checkpoint" to the
 * LangGraph server, while {} triggers a fresh plan_resolver pass over the
 * existing thread state (proved by the M2 smoke).
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { getLangGraphClient } from '@/api/langgraphClient'
import { queryKeys } from '@/api/queryKeys'

import { ALL_AUTO_GATES, recommendedRecursionLimit } from './launchRun'

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

export interface RerunConfigurable extends Record<string, unknown> {
  phases: PhaseName[]
  gates?: Record<PhaseName, UniformGatePolicy>
  limits?: { poll_interval_s?: number; poll_timeout_s?: number }
}

/**
 * Configurable for a phase-subset re-run. The previous run's persisted
 * effective config is the base; `inherit` retains its gate matrix while the
 * two explicit modes replace only that matrix.
 */
export function buildRerunConfigurable(
  phases: PhaseName[],
  gatesMode: GatesMode,
  baseConfigurable: Record<string, unknown> = {},
): RerunConfigurable {
  const configurable: RerunConfigurable = { ...baseConfigurable, phases }
  if (gatesMode === 'gated') configurable.gates = ALL_GATED_GATES
  if (gatesMode === 'auto') configurable.gates = ALL_AUTO_GATES
  return configurable
}

export interface RerunInput {
  threadId: string
  /** Canonical-order phase subset (configurable.phases). */
  phases: PhaseName[]
  gatesMode: GatesMode
  /** Effective config persisted by plan_resolver on the previous run. */
  baseConfigurable?: Record<string, unknown>
}

export interface RerunResult {
  threadId: string
  runId: string
}

async function rerunPhases({
  threadId,
  phases,
  gatesMode,
  baseConfigurable,
}: RerunInput): Promise<RerunResult> {
  const client = await getLangGraphClient()
  const configurable = buildRerunConfigurable(phases, gatesMode, baseConfigurable)
  const assistantId =
    typeof baseConfigurable?.['assistant_id'] === 'string'
      ? baseConfigurable['assistant_id']
      : 'pipeline'
  const run = await client.runs.create(threadId, assistantId, {
    input: {},
    config: { recursion_limit: recommendedRecursionLimit(configurable), configurable },
    streamMode: ['updates', 'messages-tuple', 'custom'],
    streamSubgraphs: true,
    streamResumable: true,
    durability: 'sync',
    multitaskStrategy: 'reject',
  })
  return { threadId, runId: run.run_id }
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
