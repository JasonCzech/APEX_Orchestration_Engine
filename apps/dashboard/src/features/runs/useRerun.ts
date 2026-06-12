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

import { ALL_AUTO_GATES } from './launchRun'

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

export interface RerunConfigurable {
  phases: PhaseName[]
  gates?: Record<PhaseName, UniformGatePolicy>
}

/**
 * configurable for a phase-subset re-run. `inherit` OMITS gates entirely so
 * the assistant/backend defaults apply (configurable.py GatePolicy = GATED).
 */
export function buildRerunConfigurable(
  phases: PhaseName[],
  gatesMode: GatesMode,
): RerunConfigurable {
  if (gatesMode === 'gated') return { phases, gates: ALL_GATED_GATES }
  if (gatesMode === 'auto') return { phases, gates: ALL_AUTO_GATES }
  return { phases }
}

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

async function rerunPhases({ threadId, phases, gatesMode }: RerunInput): Promise<RerunResult> {
  const client = await getLangGraphClient()
  const run = await client.runs.create(threadId, 'pipeline', {
    input: {},
    config: { configurable: { ...buildRerunConfigurable(phases, gatesMode) } },
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
