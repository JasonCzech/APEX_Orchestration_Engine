/**
 * useWizardLaunch — the wizard's launch path. Extends D2's minimal launch
 * (features/runs/launchRun.ts): same thread-create + background-run-create
 * SDK calls and identical stream options, but the configurable comes from the
 * full WizardDraft via buildLaunchPreview (the SAME builder the review step
 * renders, so the JSON shown is byte-for-byte what is sent).
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { getLangGraphClient } from '@/api/langgraphClient'
import { queryKeys } from '@/api/queryKeys'
import { recommendedRecursionLimit, type LaunchedRun } from '@/features/runs/launchRun'

import { buildLaunchPreview, type WizardDraft } from './wizardState'

export async function launchWizardRun(draft: WizardDraft): Promise<LaunchedRun> {
  const preview = buildLaunchPreview(draft)
  const client = await getLangGraphClient()
  const thread = await client.threads.create({ metadata: preview.metadata })
  // Stream options mirror D2's launchRun (plan Part 1 "Streaming" launch defaults).
  const run = await client.runs.create(thread.thread_id, 'pipeline', {
    input: preview.input,
    config: {
      recursion_limit: recommendedRecursionLimit(preview.configurable),
      configurable: preview.configurable,
    },
    streamMode: ['updates', 'messages-tuple', 'custom'],
    streamSubgraphs: true,
    streamResumable: true,
    durability: 'sync',
    multitaskStrategy: 'reject',
  })
  return { threadId: thread.thread_id, runId: run.run_id }
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
