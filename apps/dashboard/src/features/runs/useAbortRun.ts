/**
 * useAbortRun — abort a BUSY run (no gate open). External engines go through
 * the engine kill switch, which stops provider work before cancelling the
 * graph; simulator/graph-only runs use the pipeline abort endpoint. Gate-state
 * aborts keep going through the gate machine's resume path.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

async function abortGraphRun(threadId: string): Promise<void> {
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

async function abortRun(threadId: string): Promise<void> {
  // Always try the engine kill switch first. The compact pipeline summary can
  // lack engine metadata even though the backend can recover a handle from
  // nested checkpoint state or the durable projection.
  const { error, response } = await getApexClient().POST(
    '/v1/engines/runs/{thread_id}/abort',
    {
      params: { path: { thread_id: threadId } },
      body: { reason: 'Aborted from the dashboard' },
    },
  )
  if (response.ok) return
  // No discoverable engine handle means this is a graph-only/pre-execution
  // run; cancel it through the pipeline facade. Provider failures stay visible.
  if (response.status !== 404) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `External engine abort failed (${response.status})`),
      error,
    )
  }
  await abortGraphRun(threadId)
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
