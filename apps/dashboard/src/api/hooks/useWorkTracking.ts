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

export type WorkItem = components['schemas']['WorkItem']
export type ResolvedWorkItem = components['schemas']['ResolvedWorkItem']
export type WorkItemPage = components['schemas']['ResolvedWorkItemPage']
export type WorkItemDraft = components['schemas']['WorkItemDraft']
export type ProviderQuery = components['schemas']['ExecutableTranslatedQuery']
export type TranslatedQuery = components['schemas']['ResolvedTranslatedQuery']
export type WorkTrackingBinding = components['schemas']['WorkTrackingBindingOut']
export type Enrichment = components['schemas']['Enrichment']
export type SavedQuery = components['schemas']['SavedQueryOut']
export type SavedQueryCreate = components['schemas']['SavedQueryCreate']
export type SavedQueryUpdate = components['schemas']['SavedQueryUpdate']

export function workItemEnrichMutationKey(connectionId: string, key: string) {
  return ['work-items', 'enrich', connectionId, key] as const
}

export function workItemEnrichMutationScopeId(connectionId: string, key: string): string {
  return `work-items:enrich:${JSON.stringify([connectionId, key])}`
}

export function savedQueryWriteMutationKey(savedQueryId: string) {
  return ['work-items', 'saved-queries', 'write', savedQueryId] as const
}

export function savedQueryWriteMutationScopeId(savedQueryId: string): string {
  return `work-items:saved-queries:write:${savedQueryId}`
}

export interface WorkTrackingScope {
  connectionId?: string
  project?: string
}

function requireBinding<T extends { connection_id?: unknown; provider?: unknown }>(
  value: T,
  {
    expectedConnectionId,
    expectedProvider,
  }: { expectedConnectionId?: string; expectedProvider?: string } = {},
): T & { connection_id: string; provider: string } {
  if (
    typeof value.connection_id !== 'string' ||
    value.connection_id.trim() === '' ||
    typeof value.provider !== 'string' ||
    value.provider.trim() === ''
  ) {
    throw new Error('Work-tracking response omitted its resolved connection binding.')
  }
  if (expectedConnectionId && value.connection_id !== expectedConnectionId) {
    throw new Error('Work-tracking response changed the requested connection binding.')
  }
  if (expectedProvider && value.provider.toLowerCase() !== expectedProvider.toLowerCase()) {
    throw new Error('Work-tracking response changed the requested provider binding.')
  }
  return value as T & { connection_id: string; provider: string }
}

export async function fetchWorkTrackingBinding({
  project,
  connectionId,
}: WorkTrackingScope = {}): Promise<WorkTrackingBinding> {
  const { data, error, response } = await getApexClient().GET('/v1/work-tracking/binding', {
    params: {
      query: {
        ...(project ? { project } : {}),
        ...(connectionId ? { connection_id: connectionId } : {}),
      },
    },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Work-tracking binding failed (${response.status})`),
      error,
    )
  }
  return requireBinding(data, { expectedConnectionId: connectionId })
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
              ...(project ? { project } : {}),
            },
          },
          body: { text, ...(connectionId ? { connection_id: connectionId } : {}) },
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Query translate failed (${response.status})`),
          error,
        )
      }
      return requireBinding(data, { expectedConnectionId: connectionId })
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
    {
      query: ProviderQuery
      limit?: number
      offset?: number
    } & WorkTrackingScope
  >({
    mutationFn: async ({ query, limit = 25, offset = 0, connectionId, project }) => {
      if (
        connectionId &&
        query.connection_id &&
        connectionId !== query.connection_id
      ) {
        throw new Error('Conflicting work-tracking connection bindings.')
      }
      const selectedConnectionId =
        connectionId ?? query.connection_id ?? undefined
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/query/execute',
        {
          params: { query: { ...(project ? { project } : {}) } },
          body: {
            query: {
              provider: query.provider,
              query: query.query,
              confidence: query.confidence,
            },
            ...(selectedConnectionId
              ? { connection_id: selectedConnectionId }
              : {}),
            limit,
            offset,
          },
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Query execute failed (${response.status})`),
          error,
        )
      }
      return requireBinding(data, {
        expectedConnectionId: selectedConnectionId,
        expectedProvider: query.provider,
      })
    },
  })
}

/** Validate-on-add lookup for direct key entry (GET /v1/work-tracking/items/{key}). */
export async function fetchWorkItem({
  key,
  project,
  connectionId,
  expectedProvider,
}: {
  key: string
  project?: string
  connectionId?: string
  expectedProvider?: string
}): Promise<ResolvedWorkItem> {
  const { data, error, response } = await getApexClient().GET('/v1/work-tracking/items/{key}', {
    params: {
      path: { key },
      query: {
        ...(project ? { project } : {}),
        ...(connectionId ? { connection_id: connectionId } : {}),
      },
    },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Work item ${key} not found (${response.status})`),
      error,
    )
  }
  return requireBinding(data, {
    expectedConnectionId: connectionId,
    expectedProvider,
  })
}

async function fetchSavedQueries(signal?: AbortSignal): Promise<SavedQuery[]> {
  return fetchAllOffsetPages({
    label: 'Saved queries',
    pageSize: 200,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET(
        '/v1/work-tracking/saved-queries',
        {
          params: { query: { limit, offset } },
          signal,
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Saved queries request failed (${response.status})`),
          error,
        )
      }
      return data.items
    },
  })
}

/** Saved provider queries (wizard Work-items step quick picks). */
export function useSavedQueries(): UseQueryResult<SavedQuery[], Error> {
  return useQuery({
    queryKey: queryKeys.workItems.savedQueries(),
    queryFn: ({ signal }) => fetchSavedQueries(signal),
    staleTime: 60_000,
  })
}

/* ── D6 appends — work-items console + detail (plan Part 2 route table) ──── */

/** One work item by key (detail page; GET /v1/work-tracking/items/{key}). */
export function useWorkItem(
  key: string | undefined,
  project?: string,
  connectionId?: string,
  expectedProvider?: string,
): UseQueryResult<ResolvedWorkItem, Error> {
  return useQuery({
    queryKey: queryKeys.workItems.key(
      key ?? '',
      project,
      connectionId,
      expectedProvider,
    ),
    queryFn: () =>
      fetchWorkItem({
        key: key ?? '',
        ...(project ? { project } : {}),
        ...(connectionId ? { connectionId } : {}),
        ...(expectedProvider ? { expectedProvider } : {}),
      }),
    enabled: Boolean(key),
    staleTime: 30_000,
  })
}

/** Create a tracker item (operator+; POST /v1/work-tracking/items). */
export interface CreateWorkItemInput {
  body: WorkItemDraft
  project?: string
  connectionId: string
  idempotencyKey: string
}

export function createWorkItemMutationKey(prefix: 'create' | 'enrich'): string {
  return typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function'
    ? `${prefix}-${crypto.randomUUID()}`
    : `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function useCreateWorkItem(): UseMutationResult<
  ResolvedWorkItem,
  Error,
  CreateWorkItemInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ body, project, connectionId, idempotencyKey }: CreateWorkItemInput) => {
      const { data, error, response } = await getApexClient().POST('/v1/work-tracking/items', {
        params: {
          query: {
            connection_id: connectionId,
            ...(project ? { project } : {}),
          },
          header: { 'Idempotency-Key': idempotencyKey },
        },
        body,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Work item create failed (${response.status})`),
          error,
        )
      }
      return requireBinding(data, { expectedConnectionId: connectionId })
    },
    onSuccess: (created, { project }) => {
      // Seed the detail cache so the post-create navigation paints instantly.
      queryClient.setQueryData(
        queryKeys.workItems.key(
          created.key,
          project,
          created.connection_id,
          created.provider,
        ),
        created,
      )
    },
  })
}

export interface EnrichWorkItemInput {
  key: string
  body: Enrichment
  project?: string
  connectionId: string
  idempotencyKey: string
}

/** Push fields/comment onto an item (operator+; POST items/{key}/enrich). */
export function useEnrichWorkItem(
  connectionId: string,
  key: string,
): UseMutationResult<
  ResolvedWorkItem,
  Error,
  EnrichWorkItemInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: workItemEnrichMutationKey(connectionId, key),
    scope: { id: workItemEnrichMutationScopeId(connectionId, key) },
    mutationFn: async ({ key, body, project, connectionId, idempotencyKey }) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/work-tracking/items/{key}/enrich',
        {
          params: {
            path: { key },
            query: {
              connection_id: connectionId,
              ...(project ? { project } : {}),
            },
            header: { 'Idempotency-Key': idempotencyKey },
          },
          body,
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Work item enrich failed (${response.status})`),
          error,
        )
      }
      return requireBinding(data, { expectedConnectionId: connectionId })
    },
    onSuccess: (updated, { key, project }) => {
      // The response is the refreshed item — replace every detail identity that
      // can be mounted for this pinned connection. Generic `/tracker/` routes
      // intentionally omit the expected provider from their query key.
      queryClient.setQueryData(
        queryKeys.workItems.key(
          key,
          project,
          updated.connection_id,
          updated.provider,
        ),
        updated,
      )
      queryClient.setQueryData(
        queryKeys.workItems.key(key, project, updated.connection_id),
        updated,
      )
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
      void queryClient.invalidateQueries({
        queryKey: queryKeys.workItems.savedQueries(),
      })
    },
  })
}

export interface UpdateSavedQueryInput {
  savedQueryId: string
  body: SavedQueryUpdate
}

export function useUpdateSavedQuery(
  savedQueryId: string,
): UseMutationResult<SavedQuery, Error, UpdateSavedQueryInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: savedQueryWriteMutationKey(savedQueryId),
    scope: { id: savedQueryWriteMutationScopeId(savedQueryId) },
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
      void queryClient.invalidateQueries({
        queryKey: queryKeys.workItems.savedQueries(),
      })
    },
  })
}

export function useDeleteSavedQuery(
  savedQueryId: string,
): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: savedQueryWriteMutationKey(savedQueryId),
    scope: { id: savedQueryWriteMutationScopeId(savedQueryId) },
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
      void queryClient.invalidateQueries({
        queryKey: queryKeys.workItems.savedQueries(),
      })
    },
  })
}
