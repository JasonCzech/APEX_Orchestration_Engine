/**
 * /v1/context hooks (D6 — /context screen). Named useContextApi because a
 * `useContext` module would collide with React's hook in imports.
 *
 * Summaries are fire-and-forget 202s (run_id + stream_url); the screen keeps
 * a session-local history instead of polling — same contract as the prompt
 * playground. Evidence is a plain filtered read.
 */
import { useMutation, useQuery, type UseMutationResult, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type ContextSummaryRequest = components['schemas']['ContextSummaryRequest']
export type ContextSummaryAccepted = components['schemas']['ContextSummaryAccepted']
export type EvidencePacket = components['schemas']['EvidencePacket']

/**
 * The 202 carries no thread_id field — only a LangGraph stream URL shaped
 * like /threads/{thread_id}/runs/{run_id}/stream. Best-effort extraction so
 * the accepted card can deep-link to /runs/{thread_id}; null when the URL
 * doesn't match.
 */
export function threadIdFromStreamUrl(streamUrl: string | null | undefined): string | null {
  if (!streamUrl) return null
  const match = /\/threads\/([^/]+)\//.exec(streamUrl)
  return match?.[1] ?? null
}

/** Kick off a context-summary run (operator+; POST /v1/context/summaries -> 202). */
export function useCreateSummary(): UseMutationResult<
  ContextSummaryAccepted,
  Error,
  ContextSummaryRequest
> {
  return useMutation({
    mutationFn: async (body: ContextSummaryRequest) => {
      const { data, error, response } = await getApexClient().POST('/v1/context/summaries', {
        body,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Summary request failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

async function fetchEvidence(project?: string, threadId?: string): Promise<EvidencePacket[]> {
  const { data, error, response } = await getApexClient().GET('/v1/context/evidence', {
    params: {
      query: {
        ...(project ? { project } : {}),
        ...(threadId ? { thread_id: threadId } : {}),
      },
    },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Evidence request failed (${response.status})`),
      error,
    )
  }
  return data
}

/** Evidence packets accrued by runs (GET /v1/context/evidence). */
export function useEvidence(
  project?: string,
  threadId?: string,
): UseQueryResult<EvidencePacket[], Error> {
  return useQuery({
    queryKey: queryKeys.context.evidence({ project: project ?? null, thread: threadId ?? null }),
    queryFn: () => fetchEvidence(project, threadId),
    staleTime: 30_000,
  })
}
