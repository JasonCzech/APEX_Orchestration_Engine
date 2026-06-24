import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { getLangGraphClient } from '@/api/langgraphClient'

/**
 * Minimal D2 launch (full 6-step wizard arrives in D4): create a thread with
 * project metadata, then a background run on the `pipeline` assistant.
 *
 * Gates are forced ALL-AUTO for every phase — the backend defaults every gate
 * to GATED (src/apex/graphs/pipeline/configurable.py GatePolicy), and the gate
 * review UX only lands in D3, so a D2 launch must not interrupt.
 *
 * Stream options mirror the plan's launch defaults (Part 1 "Streaming"):
 * durability sync, resumable stream, multitask reject, custom+updates+
 * messages-tuple modes with subgraph events (phase nodes are subgraphs).
 */

interface GatePolicy {
  prompt_review: 'auto'
  output_review: 'auto'
}

export const ALL_AUTO_GATES: Record<PhaseName, GatePolicy> = Object.fromEntries(
  PHASE_NAMES.map((phase) => [phase, { prompt_review: 'auto', output_review: 'auto' }]),
) as Record<PhaseName, GatePolicy>

const SPINE_SUPERSTEPS = 16
const RECURSION_HEADROOM = 25
const DEFAULT_POLL_INTERVAL_S = 5
const DEFAULT_POLL_TIMEOUT_S = 4 * 3600

interface RecursionLimitConfigurable {
  limits?: {
    poll_interval_s?: number
    poll_timeout_s?: number
  }
}

export function recommendedRecursionLimit(configurable: RecursionLimitConfigurable = {}): number {
  const limits = configurable.limits ?? {}
  const interval = Math.max(limits.poll_interval_s ?? DEFAULT_POLL_INTERVAL_S, 1e-9)
  const timeout = limits.poll_timeout_s ?? DEFAULT_POLL_TIMEOUT_S
  return Math.ceil(timeout / interval) + SPINE_SUPERSTEPS + RECURSION_HEADROOM
}

export interface LaunchRunInput {
  title: string
  request: string
  projectId: string
}

export interface LaunchedRun {
  threadId: string
  runId: string
}

export async function launchRun(input: LaunchRunInput): Promise<LaunchedRun> {
  const client = await getLangGraphClient()
  const thread = await client.threads.create({
    metadata: { project_id: input.projectId },
  })
  const run = await client.runs.create(thread.thread_id, 'pipeline', {
    input: { title: input.title, request: input.request },
    config: {
      recursion_limit: recommendedRecursionLimit(),
      configurable: {
        project_id: input.projectId,
        gates: ALL_AUTO_GATES,
      },
    },
    streamMode: ['updates', 'messages-tuple', 'custom'],
    streamSubgraphs: true,
    streamResumable: true,
    durability: 'sync',
    multitaskStrategy: 'reject',
  })
  return { threadId: thread.thread_id, runId: run.run_id }
}
