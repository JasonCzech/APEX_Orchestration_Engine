import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components, operations } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type AgentAnalytics = components['schemas']['AgentAnalyticsResponse']
export type AgentAnalyticsBreakdownRow = components['schemas']['AgentAnalyticsBreakdownRow']
export type AgentAnalyticsSeriesPoint = components['schemas']['AgentAnalyticsSeriesPoint']

export type AgentGroupBy = AgentAnalytics['window']['group_by']
export type AgentSort =
  NonNullable<NonNullable<operations['getAgentAnalytics']['parameters']['query']>['sort']>
export type AgentOrder =
  NonNullable<NonNullable<operations['getAgentAnalytics']['parameters']['query']>['order']>

export interface AgentAnalyticsQuery {
  from?: string
  to?: string
  bucket?: 'day' | 'hour'
  group_by?: AgentGroupBy
  project?: string
  model?: string[]
  stage?: string[]
  agent?: string[]
  test?: string
  status?: 'ok' | 'error'
  sort?: AgentSort
  order?: AgentOrder
  limit?: number
  offset?: number
}

function normalizeQuery(params: AgentAnalyticsQuery): AgentAnalyticsQuery {
  return {
    ...(params.from ? { from: params.from } : {}),
    ...(params.to ? { to: params.to } : {}),
    ...(params.bucket ? { bucket: params.bucket } : {}),
    ...(params.group_by ? { group_by: params.group_by } : {}),
    ...(params.project ? { project: params.project } : {}),
    ...(params.model?.length ? { model: params.model } : {}),
    ...(params.stage?.length ? { stage: params.stage } : {}),
    ...(params.agent?.length ? { agent: params.agent } : {}),
    ...(params.test ? { test: params.test } : {}),
    ...(params.status ? { status: params.status } : {}),
    ...(params.sort ? { sort: params.sort } : {}),
    ...(params.order ? { order: params.order } : {}),
    ...(params.limit !== undefined ? { limit: params.limit } : {}),
    ...(params.offset ? { offset: params.offset } : {}),
  }
}

async function fetchAgentAnalytics(query: AgentAnalyticsQuery): Promise<AgentAnalytics> {
  const { data, error, response } = await getApexClient().GET('/v1/analytics/agents', {
    params: { query },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Agent analytics request failed (${response.status})`),
      error,
    )
  }
  return data
}

export function useAgentAnalytics(
  params: AgentAnalyticsQuery = {},
): UseQueryResult<AgentAnalytics, Error> {
  const query = normalizeQuery(params)
  return useQuery({
    queryKey: queryKeys.analytics.agents(query as Record<string, unknown>),
    queryFn: () => fetchAgentAnalytics(query),
    staleTime: 30_000,
  })
}
