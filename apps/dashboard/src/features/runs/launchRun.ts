import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import {
  getDurableIdempotencyKey,
  PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
  retireDurableIdempotencyKey,
} from '@/utils/durableIdempotency'

/**
 * Minimal D2 launch (full 6-step wizard arrives in D4): create a thread with
 * project metadata, then a background run on the `pipeline` assistant.
 *
 * Gates are forced ALL-AUTO for every phase — the backend defaults every gate
 * to GATED (src/apex/graphs/pipeline/configurable.py GatePolicy), and the gate
 * review UX only lands in D3, so a D2 launch must not interrupt.
 *
 * The /v1 launch facade creates a resumable, durable run and returns its
 * custom-only stream URL. Durable state is read from the scoped /v1 snapshot;
 * the stream carries bounded live pipeline events only.
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
  appId?: string
}

export interface LaunchedRun {
  threadId: string
  runId: string
}

function createLaunchIdempotencyKey(): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? crypto.randomUUID()
    : `launch-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export async function launchRun(input: LaunchRunInput): Promise<LaunchedRun> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const requestPayload = {
    title: input.title,
    request: input.request,
    project_id: input.projectId,
    ...(input.appId ? { app_id: input.appId } : {}),
    configurable: {
      project_id: input.projectId,
      ...(input.appId ? { app_id: input.appId } : {}),
      gates: ALL_AUTO_GATES,
    },
  }
  const idempotencyKey = await getDurableIdempotencyKey(
    PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
    requestPayload,
    createLaunchIdempotencyKey,
  )
  if (keyRevision !== getApiKeyRevision() || sessionRevision !== getSessionRevision()) {
    throw new Error('Credentials changed while preparing the launch; please retry.')
  }
  const { data, error, response } = await getApexClient().POST('/v1/pipelines', {
    body: {
      idempotency_key: idempotencyKey,
      ...requestPayload,
    },
  })
  if (keyRevision !== getApiKeyRevision() || sessionRevision !== getSessionRevision()) {
    throw new Error('Credentials changed while launching the run; retry to recover its result.')
  }
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Pipeline launch failed (${response.status})`),
      error,
    )
  }
  await retireDurableIdempotencyKey(
    PIPELINE_LAUNCH_IDEMPOTENCY_STORAGE_KEY,
    requestPayload,
    idempotencyKey,
  )
  return { threadId: data.thread_id, runId: data.run_id }
}
