import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type UsageAnalytics = components['schemas']['UsageAnalyticsResponse']
export type UsageBucket = components['schemas']['UsageBucket']
export type UsageTopAction = components['schemas']['UsageTopAction']

/** Query params accepted by GET /v1/analytics/usage. */
export interface UsageAnalyticsQuery {
  /** Window start (ISO-8601); server default = `to` minus 7 days. */
  from?: string
  /** Window end (ISO-8601, exclusive); server default = now. */
  to?: string
  bucket?: 'day' | 'hour'
  project?: string
}

/** Drops unset params so the query key (and the wire format) stay canonical. */
function normalizeQuery(params: UsageAnalyticsQuery): UsageAnalyticsQuery {
  return {
    ...(params.from ? { from: params.from } : {}),
    ...(params.to ? { to: params.to } : {}),
    ...(params.bucket ? { bucket: params.bucket } : {}),
    ...(params.project ? { project: params.project } : {}),
  }
}

async function fetchUsageAnalytics(query: UsageAnalyticsQuery): Promise<UsageAnalytics> {
  const { data, error, response } = await getApexClient().GET('/v1/analytics/usage', {
    params: { query },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Usage analytics request failed (${response.status})`),
      error,
    )
  }
  return data
}

/**
 * Usage aggregates on `queryKeys.analytics.usage(params)` (D6 /analytics).
 * keepPreviousData keeps the previous window rendered while a new one loads
 * (preset/bucket switches feel instant); aggregates are cheap to recompute
 * server-side, so a short staleTime keeps the screen honest.
 */
export function useUsageAnalytics(
  params: UsageAnalyticsQuery = {},
): UseQueryResult<UsageAnalytics, Error> {
  const query = normalizeQuery(params)
  return useQuery({
    queryKey: queryKeys.analytics.usage(query as Record<string, unknown>),
    queryFn: () => fetchUsageAnalytics(query),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
  })
}
