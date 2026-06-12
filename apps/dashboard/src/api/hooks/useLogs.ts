import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type LogEntry = components['schemas']['LogEntryOut']
export type LogSearchResponse = components['schemas']['LogSearchResponse']
type LogSearchRequest = components['schemas']['LogSearchRequest']

/**
 * Flattened search input the LogsPage constructs on explicit submit.
 * `filters` are the backend's ANDed exact-match terms (service, level,
 * thread_id by convention); from/to become the request `window`.
 */
export interface LogSearchInput {
  text?: string
  filters?: Record<string, string>
  from?: string
  to?: string
  limit: number
  offset: number
}

function toRequestBody(input: LogSearchInput): LogSearchRequest {
  return {
    query: {
      ...(input.text ? { text: input.text } : {}),
      filters: input.filters ?? {},
    },
    ...(input.from || input.to
      ? { window: { from: input.from ?? null, to: input.to ?? null } }
      : {}),
    limit: input.limit,
    offset: input.offset,
  }
}

async function searchLogs(input: LogSearchInput): Promise<LogSearchResponse> {
  const { data, error, response } = await getApexClient().POST('/v1/logs/search', {
    body: toRequestBody(input),
  })
  if (!response.ok || !data) {
    // 422 = provider rejected the query (ES reason in detail); 502 = upstream
    // transport failure. The page branches on ApiError.status for both.
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Log search failed (${response.status})`),
      error,
    )
  }
  return data
}

/**
 * Log search on `queryKeys.logs.search(input)` (D6 /logs).
 *
 * POST is a read here (search body), so it still flows through useQuery —
 * but it is submit-only by construction: the page passes `null` until the
 * user explicitly submits (or deep-links with URL filters), so no request
 * fires while typing. Pagination re-keys the same submitted input with a new
 * offset; keepPreviousData keeps the previous page rendered meanwhile.
 */
export function useLogSearch(
  input: LogSearchInput | null,
): UseQueryResult<LogSearchResponse, Error> {
  return useQuery({
    queryKey: queryKeys.logs.search((input ?? {}) as Record<string, unknown>),
    queryFn: () => {
      if (!input) throw new Error('log search requires a submitted query')
      return searchLogs(input)
    },
    enabled: input !== null,
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}
