/**
 * useWizardLaunch — the wizard's launch path through the domain facade. The
 * server resolves selected document ids into full context packets before it
 * creates the thread, while selected work items are resolved here into inline
 * packets. The full configurable bundle and selected assistant id are retained.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchWorkItem } from '@/api/hooks/useWorkTracking'
import { queryKeys } from '@/api/queryKeys'
import type { LaunchedRun } from '@/features/runs/launchRun'

import { buildLaunchPreview, type WizardDraft } from './wizardState'

type ContextPacketInput = NonNullable<
  components['schemas']['StartPipelineRequest']['context_packets']
>[number]

async function resolveWorkItemPackets(draft: WizardDraft): Promise<ContextPacketInput[]> {
  const project = draft.scope.project_id.trim() || undefined
  const workItems = await Promise.all(
    draft.work_item_keys.map((key) => fetchWorkItem(key, project)),
  )
  const packets: ContextPacketInput[] = workItems.map((item) => ({
    id: `workitem-${item.key}`,
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

export async function launchWizardRun(draft: WizardDraft): Promise<LaunchedRun> {
  const preview = buildLaunchPreview(draft)
  const contextPackets = await resolveWorkItemPackets(draft)
  const { data, error, response } = await getApexClient().POST('/v1/pipelines', {
    body: {
      assistant_id: preview.assistant_id,
      title: preview.input.title,
      request: preview.input.request,
      project_id: draft.scope.project_id.trim(),
      ...(draft.scope.app_id ? { app_id: draft.scope.app_id } : {}),
      configurable: preview.configurable,
      ...(preview.document_ids.length > 0 ? { document_ids: preview.document_ids } : {}),
      ...(contextPackets.length > 0 ? { context_packets: contextPackets } : {}),
    },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Pipeline launch failed (${response.status})`),
      error,
    )
  }
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
