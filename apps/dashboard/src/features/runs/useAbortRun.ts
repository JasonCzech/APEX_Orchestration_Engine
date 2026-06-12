/**
 * useAbortRun — abort a BUSY run (no gate open) via the domain façade's
 * POST /v1/pipelines/{thread_id}/abort (operationId abortPipeline, 202),
 * which cancels the thread's active LangGraph run(s). Gate-state aborts keep
 * going through the gate machine's resume path (useGate submit('abort')) —
 * this hook covers the D8 parity gap where a mid-phase run had no abort.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

async function abortRun(threadId: string): Promise<void> {
  const { error, response } = await getApexClient().POST('/v1/pipelines/{thread_id}/abort', {
    params: { path: { thread_id: threadId } },
  })
  if (!response.ok) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Abort failed (${response.status})`),
      error,
    )
  }
}

/** 2xx invalidates the thread snapshot + pipelines lists (status flips fast). */
export function useAbortRun(threadId: string) {
  const queryClient = useQueryClient()
  return useMutation<void, Error, void>({
    mutationFn: () => abortRun(threadId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(threadId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.pipelines.lists() })
    },
  })
}
