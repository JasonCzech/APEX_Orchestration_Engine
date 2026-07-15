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
import { queryKeys } from '@/api/queryKeys'

export type WorkItem = components['schemas']['WorkItem']
export type WorkItemPage = components['schemas']['WorkItemPage']
export type WorkItemDraft = components['schemas']['WorkItemDraft']
export type TranslatedQuery = components['schemas']['TranslatedQuery']
export type Enrichment = components['schemas']['Enrichment']
export type SavedQuery = components['schemas']['SavedQueryOut']
export type SavedQueryCreate = components['schemas']['SavedQueryCreate']
export type SavedQueryUpdate = components['schemas']['SavedQueryUpdate']

export interface WorkTrackingScope {
  connectionId?: string
  project?: string
}

/** NL -> provider query (POST /v1/work-tracking/query/translate). */
export function useTranslateQuery() {
  return useMutation<TranslatedQuery, Error, { text: string } & WorkTrackingScope>({
    mutationFn: async ({ text, connectionId, project }) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/query/translate',
        {
          params: {
            query: {
              ...(connectionId ? { connection_id: connectionId } : {}),
              ...(project ? { project } : {}),
            },
          },
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
  // D6 extension: optional offset so the console can paginate (default 0
  // preserves the wizard's call shape).
  return useMutation<
    WorkItemPage,
    Error,
    { query: TranslatedQuery; limit?: number; offset?: number } & WorkTrackingScope
  >({
    mutationFn: async ({ query, limit = 25, offset = 0, connectionId, project }) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/query/execute',
        {
          params: {
            query: {
              ...(connectionId ? { connection_id: connectionId } : {}),
              ...(project ? { project } : {}),
            },
          },
          body: { query, limit, offset },
        },
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
export async function fetchWorkItem(key: string, project?: string): Promise<WorkItem> {
  const { data, error, response } = await getApexClient().GET('/v1/work-tracking/items/{key}', {
    params: { path: { key }, query: project ? { project } : {} },
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
  const items: SavedQuery[] = []
  const limit = 200
  for (let offset = 0; ; offset += limit) {
    const { data, error, response } = await getApexClient().GET(
      '/v1/work-tracking/saved-queries',
      { params: { query: { limit, offset } } },
    )
    if (!response.ok || !data) {
      throw new ApiError(
        response.status,
        errorMessageOf(error, `Saved queries request failed (${response.status})`),
        error,
      )
    }
    items.push(...data.items)
    if (data.items.length < limit) return items
  }
}

/** Saved provider queries (wizard Work-items step quick picks). */
export function useSavedQueries(): UseQueryResult<SavedQuery[], Error> {
  return useQuery({
    queryKey: queryKeys.workItems.savedQueries(),
    queryFn: fetchSavedQueries,
    staleTime: 60_000,
  })
}

/* ── D6 appends — work-items console + detail (plan Part 2 route table) ──── */

/** One work item by key (detail page; GET /v1/work-tracking/items/{key}). */
export function useWorkItem(
  key: string | undefined,
  project?: string,
): UseQueryResult<WorkItem, Error> {
  return useQuery({
    queryKey: queryKeys.workItems.key(key ?? '', project),
    queryFn: () => fetchWorkItem(key ?? '', project),
    enabled: Boolean(key),
    staleTime: 30_000,
  })
}

/** Create a tracker item (operator+; POST /v1/work-tracking/items). */
export interface CreateWorkItemInput {
  body: WorkItemDraft
  project?: string
}

export function useCreateWorkItem(): UseMutationResult<WorkItem, Error, CreateWorkItemInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ body, project }: CreateWorkItemInput) => {
      const { data, error, response } = await getApexClient().POST('/v1/work-tracking/items', {
        params: { query: project ? { project } : {} },
        body,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Work item create failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (created, { project }) => {
      // Seed the detail cache so the post-create navigation paints instantly.
      queryClient.setQueryData(queryKeys.workItems.key(created.key, project), created)
    },
  })
}

export interface EnrichWorkItemInput {
  key: string
  body: Enrichment
  project?: string
}

/** Push fields/comment onto an item (operator+; POST items/{key}/enrich). */
export function useEnrichWorkItem(): UseMutationResult<WorkItem, Error, EnrichWorkItemInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ key, body, project }: EnrichWorkItemInput) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/items/{key}/enrich',
        { params: { path: { key }, query: project ? { project } : {} }, body },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Work item enrich failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (updated, { key, project }) => {
      // The response is the refreshed item — replace the detail cache directly.
      queryClient.setQueryData(queryKeys.workItems.key(key, project), updated)
    },
  })
}

export function useCreateSavedQuery(): UseMutationResult<SavedQuery, Error, SavedQueryCreate> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: SavedQueryCreate) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/saved-queries',
        { body },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Saved query create failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.workItems.savedQueries() })
    },
  })
}

export interface UpdateSavedQueryInput {
  savedQueryId: string
  body: SavedQueryUpdate
}

export function useUpdateSavedQuery(): UseMutationResult<SavedQuery, Error, UpdateSavedQueryInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ savedQueryId, body }: UpdateSavedQueryInput) => {
      const { data, error, response } = await getApexClient().PATCH(
        '/v1/work-tracking/saved-queries/{saved_query_id}',
        { params: { path: { saved_query_id: savedQueryId } }, body },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Saved query update failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.workItems.savedQueries() })
    },
  })
}

export function useDeleteSavedQuery(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (savedQueryId: string) => {
      const { error, response } = await getApexClient().DELETE(
        '/v1/work-tracking/saved-queries/{saved_query_id}',
        { params: { path: { saved_query_id: savedQueryId } } },
      )
      if (!response.ok) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Saved query delete failed (${response.status})`),
          error,
        )
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.workItems.savedQueries() })
    },
  })
}
