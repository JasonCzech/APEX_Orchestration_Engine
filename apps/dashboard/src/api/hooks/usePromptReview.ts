import { useMutation, useQuery, useQueryClient, type UseMutationResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'
import type { PhaseName } from '@apex/pipeline-events'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type PhasePromptReview = components['schemas']['PhasePromptReview']
type PhasePromptReviewUpdate = components['schemas']['PhasePromptReviewUpdate']

export function promptReviewWriteMutationKey(threadId: string) {
  return ['threads', threadId, 'prompt-review', 'write'] as const
}

export function promptReviewWriteMutationScopeId(threadId: string): string {
  return `threads:${threadId}:prompt-review:write`
}

async function fetchPromptReview(threadId: string, phase: PhaseName): Promise<PhasePromptReview> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/pipelines/{thread_id}/phases/{phase}/prompt-review',
    {
      params: { path: { thread_id: threadId, phase } },
    },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Prompt review failed (${response.status})`),
      error,
    )
  }
  return data
}

async function patchPromptReview({
  threadId,
  phase,
  body,
}: {
  threadId: string
  phase: PhaseName
  body: PhasePromptReviewUpdate
}): Promise<PhasePromptReview> {
  const { data, error, response } = await getApexClient().PATCH(
    '/v1/pipelines/{thread_id}/phases/{phase}/prompt-review',
    {
      params: { path: { thread_id: threadId, phase } },
      body,
    },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Prompt review save failed (${response.status})`),
      error,
    )
  }
  return data
}

export function usePromptReview(threadId: string | undefined, phase: PhaseName | undefined) {
  return useQuery({
    queryKey: queryKeys.threads.promptReview(threadId ?? '', phase ?? ''),
    queryFn: () => fetchPromptReview(threadId ?? '', phase ?? 'story_analysis'),
    enabled: Boolean(threadId) && Boolean(phase),
    staleTime: STALE_TIMES.threadState,
  })
}

export function useUpdatePromptReview(threadId: string): UseMutationResult<
  PhasePromptReview,
  Error,
  {
    threadId: string
    phase: PhaseName
    body: PhasePromptReviewUpdate
  }
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: promptReviewWriteMutationKey(threadId),
    scope: { id: promptReviewWriteMutationScopeId(threadId) },
    mutationFn: patchPromptReview,
    onSuccess: (data, variables) => {
      queryClient.setQueryData(
        queryKeys.threads.promptReview(variables.threadId, variables.phase),
        data,
      )
      void queryClient.invalidateQueries({
        queryKey: queryKeys.threads.state(variables.threadId),
      })
      // The application prompt is app-wide, so a save can change every phase's
      // effective prompt — invalidate all phases' prompt-review caches, not just this one.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.threads.promptReviews(variables.threadId),
      })
    },
  })
}
