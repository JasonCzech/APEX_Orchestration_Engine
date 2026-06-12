import { useMutation, useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type WorkItem = components['schemas']['WorkItem']
export type WorkItemPage = components['schemas']['WorkItemPage']
export type TranslatedQuery = components['schemas']['TranslatedQuery']
export type SavedQuery = components['schemas']['SavedQueryOut']

/** NL -> provider query (POST /v1/work-tracking/query/translate). */
export function useTranslateQuery() {
  return useMutation<TranslatedQuery, Error, { text: string; connectionId?: string }>({
    mutationFn: async ({ text, connectionId }) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/query/translate',
        {
          params: { query: connectionId ? { connection_id: connectionId } : {} },
          body: { text },
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Query translate failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

/** Provider query -> work items page (POST /v1/work-tracking/query/execute). */
export function useExecuteQuery() {
  return useMutation<WorkItemPage, Error, { query: TranslatedQuery; limit?: number }>({
    mutationFn: async ({ query, limit = 25 }) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/query/execute',
        { body: { query, limit, offset: 0 } },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Query execute failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

/** Validate-on-add lookup for direct key entry (GET /v1/work-tracking/items/{key}). */
export async function fetchWorkItem(key: string): Promise<WorkItem> {
  const { data, error, response } = await getApexClient().GET('/v1/work-tracking/items/{key}', {
    params: { path: { key } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Work item ${key} not found (${response.status})`),
      error,
    )
  }
  return data
}

async function fetchSavedQueries(): Promise<SavedQuery[]> {
  const { data, error, response } = await getApexClient().GET('/v1/work-tracking/saved-queries', {
    params: { query: {} },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Saved queries request failed (${response.status})`),
      error,
    )
  }
  return data.items
}

/** Saved provider queries (wizard Work-items step quick picks). */
export function useSavedQueries(): UseQueryResult<SavedQuery[], Error> {
  return useQuery({
    queryKey: queryKeys.workItems.savedQueries(),
    queryFn: fetchSavedQueries,
    staleTime: 60_000,
  })
}
