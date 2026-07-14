import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

/** Poll cadence for fleet liveness (plan Part 2: pipelines list 15s, visibility-aware). */
export const PIPELINES_POLL_INTERVAL_MS = 15_000

export type PendingGate = components['schemas']['PendingGate']
export type PhaseStripEntry = components['schemas']['PhaseStripEntry']

export type PipelineSummary = components['schemas']['PipelineSummary']

export type PipelineListResponse = Omit<components['schemas']['PipelineListResponse'], 'items'> & {
  items: PipelineSummary[]
  /** Not in the contract today; honored for bounds checks if the backend adds it. */
  total?: number
}

/** Query params accepted by GET /v1/pipelines (structurally satisfied by RunsFilters). */
export interface PipelinesQuery {
  status?: 'idle' | 'busy' | 'interrupted' | 'error'
  q?: string
  project?: string
  limit?: number
  offset?: number
}

/** Drops unset params so the query key (and the wire format) stay canonical. */
function normalizeQuery(filters: PipelinesQuery): PipelinesQuery {
  return {
    ...(filters.status ? { status: filters.status } : {}),
    ...(filters.q ? { q: filters.q } : {}),
    ...(filters.project ? { project: filters.project } : {}),
    ...(filters.limit !== undefined ? { limit: filters.limit } : {}),
    ...(filters.offset ? { offset: filters.offset } : {}),
  }
}

async function fetchPipelines(query: PipelinesQuery): Promise<PipelineListResponse> {
  const { data, error, response } = await getApexClient().GET('/v1/pipelines', {
    params: { query },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Pipelines request failed (${response.status})`),
      error,
    )
  }
  return data as PipelineListResponse
}

/**
 * Runs-history list on `queryKeys.pipelines.list(filters)`.
 * keepPreviousData keeps the previous page rendered while the next loads;
 * 15s polling pauses while the tab is hidden.
 */
export function usePipelines(
  filters: PipelinesQuery = {},
): UseQueryResult<PipelineListResponse, Error> {
  const query = normalizeQuery(filters)
  return useQuery({
    queryKey: queryKeys.pipelines.list(query as Record<string, unknown>),
    queryFn: () => fetchPipelines(query),
    placeholderData: keepPreviousData,
    staleTime: STALE_TIMES.pipelinesList,
    refetchInterval: PIPELINES_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
  })
}
