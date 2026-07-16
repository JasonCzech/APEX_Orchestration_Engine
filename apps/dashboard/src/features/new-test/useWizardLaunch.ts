/**
 * useWizardLaunch — the wizard's launch path through the domain facade. The
 * server resolves selected document ids into full context packets before it
 * creates the thread, while selected work items are resolved here into inline
 * packets. The full configurable bundle and selected assistant id are retained.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchWorkItem } from '@/api/hooks/useWorkTracking'
import { queryKeys } from '@/api/queryKeys'
import type { LaunchedRun } from '@/features/runs/launchRun'
import {
  getDurableIdempotencyAttempt,
  retireDurableIdempotencyAttempt,
  stableIdempotencyPayload,
} from '@/utils/durableIdempotency'

import {
  buildLaunchPreview,
  createLaunchIdempotencyKey,
  type WizardDraft,
  type WizardWorkItemRef,
} from './wizardState'

type ContextPacketInput = NonNullable<
  components['schemas']['StartPipelineRequest']['context_packets']
>[number]

type WizardLaunchRequestPayload = Omit<
  components['schemas']['StartPipelineRequest'],
  'idempotency_key'
>

const WIZARD_LAUNCH_ATTEMPT_STORAGE_KEY =
  'apex.idempotency.wizard-pipeline-launch.v1'

type BoundWorkItemRef = WizardWorkItemRef & {
  connection_id: string
  provider: string
}

function boundWorkItemRefs(draft: WizardDraft): BoundWorkItemRef[] {
  if (draft.work_items.some((item) => !item.connection_id || !item.provider)) {
    throw new Error('Revalidate legacy work items before launching this run.')
  }
  const refs = draft.work_items as BoundWorkItemRef[]
  if (new Set(refs.map((item) => item.connection_id)).size > 1) {
    throw new Error('Selected work items must use one work-tracking connection.')
  }
  return refs
}

async function resolveWorkItemPackets(draft: WizardDraft): Promise<ContextPacketInput[]> {
  const project = draft.scope.project_id.trim() || undefined
  const refs = boundWorkItemRefs(draft)
  const workItems = await Promise.all(
    refs.map(async (ref) => {
      const item = await fetchWorkItem({
        key: ref.key,
        ...(project ? { project } : {}),
        connectionId: ref.connection_id,
        expectedProvider: ref.provider,
      })
      if (item.key !== ref.key) {
        throw new Error(`Work-tracking lookup returned ${item.key} for requested key ${ref.key}.`)
      }
      return item
    }),
  )
  const packets: ContextPacketInput[] = workItems.map((item, index) => ({
    id: `workitem-${index + 1}`,
    source: 'work_tracking',
    title: item.title,
    summary: `${item.kind} · ${item.status}`,
    ref: item.url ?? item.key,
    ...(item.description.trim().length > 0 ? { text: item.description } : {}),
  }))
  packets.push(
    ...draft.context_summary_ids.map((id) => ({
      id: `context-${id}`,
      source: 'context_summary',
      title: id,
      ref: id,
    })),
  )
  return packets
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

const CONTEXT_PACKET_KEYS = new Set(['id', 'source', 'title', 'summary', 'ref', 'text'])

function isStoredContextPacket(value: unknown): value is ContextPacketInput {
  if (!isRecord(value)) return false
  if (Object.keys(value).some((key) => !CONTEXT_PACKET_KEYS.has(key))) return false
  if (
    typeof value['id'] !== 'string' ||
    typeof value['source'] !== 'string' ||
    typeof value['title'] !== 'string'
  ) {
    return false
  }
  return ['summary', 'ref', 'text'].every(
    (key) => value[key] === undefined || typeof value[key] === 'string',
  )
}

function storedPayloadMatchesIntent(
  value: unknown,
  baseRequestPayload: WizardLaunchRequestPayload,
): value is WizardLaunchRequestPayload {
  if (!isRecord(value)) return false
  const { context_packets: contextPackets, ...storedBase } = value
  if (stableIdempotencyPayload(storedBase) !== stableIdempotencyPayload(baseRequestPayload)) {
    return false
  }
  return (
    contextPackets === undefined ||
    (Array.isArray(contextPackets) &&
      contextPackets.length > 0 &&
      contextPackets.every(isStoredContextPacket))
  )
}

export async function launchWizardRun(draft: WizardDraft): Promise<LaunchedRun> {
  const keyRevision = getApiKeyRevision()
  const sessionRevision = getSessionRevision()
  const preview = buildLaunchPreview(draft)
  const launchIntent = {
    assistant_id: preview.assistant_id,
    title: preview.input.title,
    request: preview.input.request,
    scope: {
      project_id: draft.scope.project_id.trim(),
      app_id: draft.scope.app_id,
      environment_id: draft.scope.environment_id,
    },
    configurable: preview.configurable,
    document_ids: preview.document_ids,
    work_items: draft.work_items.map((item) => ({ ...item })),
    context_summary_ids: [...draft.context_summary_ids],
  }
  const baseRequestPayload: WizardLaunchRequestPayload = {
    assistant_id: preview.assistant_id,
    title: preview.input.title,
    request: preview.input.request,
    project_id: draft.scope.project_id.trim(),
    ...(draft.scope.app_id ? { app_id: draft.scope.app_id } : {}),
    configurable: preview.configurable,
    ...(preview.document_ids.length > 0 ? { document_ids: preview.document_ids } : {}),
  }
  const { idempotencyKey, requestPayload } = await getDurableIdempotencyAttempt({
    storageKey: WIZARD_LAUNCH_ATTEMPT_STORAGE_KEY,
    intent: launchIntent,
    createKey: createLaunchIdempotencyKey,
    createRequestPayload: async () => {
      const contextPackets = await resolveWorkItemPackets(draft)
      return {
        ...baseRequestPayload,
        ...(contextPackets.length > 0 ? { context_packets: contextPackets } : {}),
      }
    },
    validateRequestPayload: (
      value,
    ): value is WizardLaunchRequestPayload =>
      storedPayloadMatchesIntent(value, baseRequestPayload),
  })
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
  await retireDurableIdempotencyAttempt(
    WIZARD_LAUNCH_ATTEMPT_STORAGE_KEY,
    launchIntent,
    idempotencyKey,
  )
  return { threadId: data.thread_id, runId: data.run_id }
}

/**
 * Launch mutation: invalidates the pipelines façade list (like D2's
 * useLaunchRun) so the new thread appears without waiting for the 15s poll.
 * Navigation + best-effort draft delete are the caller's job (NewRunWizard).
 */
export function useWizardLaunch() {
  const queryClient = useQueryClient()
  return useMutation<LaunchedRun, Error, WizardDraft>({
    mutationFn: launchWizardRun,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.all })
    },
  })
}
