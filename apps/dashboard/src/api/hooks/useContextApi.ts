/**
 * /v1/context hooks (D6 — /context screen). Named useContextApi because a
 * `useContext` module would collide with React's hook in imports.
 *
 * Summaries are fire-and-forget 202s (run_id + stream_url); the screen keeps
 * a session-local history instead of polling — same contract as the prompt
 * playground. Evidence is a plain filtered read.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages } from '@/api/fetchAllPages'
import { queryKeys } from '@/api/queryKeys'

export type ContextSummaryRequest = components['schemas']['ContextSummaryRequest']
export type ContextSummaryAccepted = components['schemas']['ContextSummaryAccepted']
export type EvidencePacket = components['schemas']['EvidencePacket']
const EVIDENCE_PAGE_SIZE = 100

export interface ContextSummaryRun {
  runId: string
  threadId: string | null
  streamUrl: string
  at: string
  subject: string
}

export function contextSummaryCreateMutationKey() {
  return ['context', 'summaries', 'create'] as const
}

export function contextSummaryCreateMutationScopeId(): string {
  return 'context-summary-create'
}

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
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: contextSummaryCreateMutationKey(),
    scope: { id: contextSummaryCreateMutationScopeId() },
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
    onSuccess: (accepted, request) => {
      const run: ContextSummaryRun = {
        runId: accepted.run_id,
        threadId: threadIdFromStreamUrl(accepted.stream_url),
        streamUrl: accepted.stream_url,
        at: new Date().toISOString(),
        subject: request.subject,
      }
      queryClient.setQueryData<ContextSummaryRun[]>(
        queryKeys.context.summaries(),
        (history = []) => [run, ...history],
      )
    },
  })
}

/**
 * Accepted handles are session-local client state. Keeping them in the query
 * cache lets a mutation publish after its route observer unmounts; the auth
 * lifecycle clears/replaces the QueryClient whenever the principal changes.
 */
export function useContextSummaryHistory(): UseQueryResult<ContextSummaryRun[], Error> {
  return useQuery({
    queryKey: queryKeys.context.summaries(),
    queryFn: (): ContextSummaryRun[] => [],
    initialData: (): ContextSummaryRun[] => [],
    // Client-owned session state: broad context invalidations must not replace
    // accepted handles with an empty synthetic fetch result.
    enabled: false,
    staleTime: Infinity,
    gcTime: Infinity,
  })
}

async function fetchEvidence(
  project?: string,
  threadId?: string,
  signal?: AbortSignal,
): Promise<EvidencePacket[]> {
  return fetchAllOffsetPages({
    label: 'Evidence',
    pageSize: EVIDENCE_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/context/evidence', {
        params: {
          query: {
            ...(project ? { project } : {}),
            ...(threadId ? { thread_id: threadId } : {}),
            limit,
            offset,
          },
        },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Evidence request failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

/** Evidence packets accrued by runs (GET /v1/context/evidence). */
export function useEvidence(
  project?: string,
  threadId?: string,
): UseQueryResult<EvidencePacket[], Error> {
  return useQuery({
    queryKey: queryKeys.context.evidence({ project: project ?? null, thread: threadId ?? null }),
    queryFn: ({ signal }) => fetchEvidence(project, threadId, signal),
    staleTime: 30_000,
  })
}
