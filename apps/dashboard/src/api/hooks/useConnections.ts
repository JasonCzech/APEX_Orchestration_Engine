/**
 * Admin connection registry over /v1/admin/connections (plan route
 * /admin/connections, D7). All endpoints are admin-role; the pages gate the UI
 * and the server enforces regardless.
 *
 * Mutations invalidate the ['admin', 'connections'] prefix, which fans out to
 * the kind-grouped list, cached details and host-mapping lists in one shot.
 * The probe (POST /{id}/test) is deliberately cache-free: it always answers
 * 200 with {ok, latency_ms, detail} and failures render inline, never as a
 * query error.
 */
import {
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages } from '@/api/fetchAllPages'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type Connection = components['schemas']['ConnectionOut']
export type ConnectionCreate = components['schemas']['ConnectionCreate']
export type ConnectionUpdate = components['schemas']['ConnectionUpdate']
export type HostMappingIn = components['schemas']['HostMappingIn']
export type HostMappingOut = components['schemas']['HostMappingOut']
export type ProbeResult = components['schemas']['ProbeResult']
export type PortKind = components['schemas']['PortKind']

/** Canonical kind order for the grouped list (matches the PortKind enum). */
export const PORT_KINDS: readonly PortKind[] = [
  'work_tracking',
  'log_search',
  'observability',
  'documents',
  'cluster_inventory',
  'source_control',
  'execution_engine',
  'artifact_store',
  'secrets',
]
const CONNECTIONS_PAGE_SIZE = 200
const deletedConnectionIds = new WeakMap<QueryClient, Set<string>>()

export function connectionWriteMutationKey(connectionId: string) {
  return ['admin', 'connections', 'write', connectionId] as const
}

export function connectionProbeMutationKey(connectionId: string) {
  return ['admin', 'connections', 'probe', connectionId] as const
}

export function connectionMutationScopeId(connectionId: string): string {
  return `admin:connections:operation:${connectionId}`
}

function deletedIdsFor(queryClient: QueryClient): Set<string> {
  const existing = deletedConnectionIds.get(queryClient)
  if (existing) return existing
  const created = new Set<string>()
  deletedConnectionIds.set(queryClient, created)
  return created
}

function isConnectionDeleted(queryClient: QueryClient, connectionId: string): boolean {
  return deletedConnectionIds.get(queryClient)?.has(connectionId) ?? false
}

async function fetchConnections(signal?: AbortSignal): Promise<Connection[]> {
  return fetchAllOffsetPages({
    label: 'Connections',
    pageSize: CONNECTIONS_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/admin/connections', {
        params: { query: { limit, offset } },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Connections request failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

async function fetchConnection(connectionId: string): Promise<Connection> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/admin/connections/{connection_id}',
    { params: { path: { connection_id: connectionId } } },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Connection request failed (${response.status})`),
      error,
    )
  }
  return data
}

async function fetchHostMappings(connectionId: string): Promise<HostMappingOut[]> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/admin/connections/{connection_id}/host-mappings',
    { params: { path: { connection_id: connectionId } } },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Host mappings request failed (${response.status})`),
      error,
    )
  }
  return data
}

/** Unfiltered connection registry — the list screen groups by kind client-side. */
export function useConnectionsIndex(): UseQueryResult<Connection[], Error> {
  return useQuery({
    queryKey: queryKeys.admin.connections(),
    queryFn: ({ signal }) => fetchConnections(signal),
    staleTime: STALE_TIMES.admin,
  })
}

export function useConnection(connectionId: string | undefined): UseQueryResult<Connection, Error> {
  return useQuery({
    queryKey: queryKeys.admin.connection(connectionId ?? ''),
    queryFn: () => fetchConnection(connectionId ?? ''),
    enabled: Boolean(connectionId),
    staleTime: STALE_TIMES.admin,
  })
}

export function useHostMappings(
  connectionId: string | undefined,
): UseQueryResult<HostMappingOut[], Error> {
  return useQuery({
    queryKey: queryKeys.admin.connectionHostMappings(connectionId ?? ''),
    queryFn: () => fetchHostMappings(connectionId ?? ''),
    enabled: Boolean(connectionId),
    staleTime: STALE_TIMES.admin,
  })
}

export function useCreateConnection(): UseMutationResult<Connection, Error, ConnectionCreate> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: ConnectionCreate) => {
      const { data, error, response } = await getApexClient().POST('/v1/admin/connections', {
        body,
      })
      if (!response.ok || !data) {
        // 422 detail carries the registered-provider list — keep it verbatim.
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Connection create failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (created) => {
      deletedIdsFor(queryClient).delete(created.id)
      queryClient.setQueryData(queryKeys.admin.connection(created.id), created)
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.connections() })
    },
  })
}

export interface UpdateConnectionInput {
  connectionId: string
  body: ConnectionUpdate
}

export function useUpdateConnection(
  connectionId: string,
): UseMutationResult<Connection, Error, UpdateConnectionInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: connectionWriteMutationKey(connectionId),
    scope: { id: connectionMutationScopeId(connectionId) },
    mutationFn: async ({ connectionId, body }: UpdateConnectionInput) => {
      const { data, error, response } = await getApexClient().PATCH(
        '/v1/admin/connections/{connection_id}',
        { params: { path: { connection_id: connectionId } }, body },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Connection update failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (updated) => {
      if (isConnectionDeleted(queryClient, updated.id)) return
      queryClient.setQueryData(queryKeys.admin.connection(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.connections() })
    },
  })
}

export function useDeleteConnection(connectionId: string): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: connectionWriteMutationKey(connectionId),
    scope: { id: connectionMutationScopeId(connectionId) },
    mutationFn: async (connectionId: string) => {
      const { error, response } = await getApexClient().DELETE(
        '/v1/admin/connections/{connection_id}',
        { params: { path: { connection_id: connectionId } } },
      )
      if (!response.ok) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Connection delete failed (${response.status})`),
          error,
        )
      }
    },
    onSuccess: (_void, connectionId) => {
      deletedIdsFor(queryClient).add(connectionId)
      queryClient.removeQueries({ queryKey: queryKeys.admin.connection(connectionId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.connections() })
    },
  })
}

export interface SetConnectionEnabledInput {
  connectionId: string
  enabled: boolean
}

/** Flips the toggle pill via the dedicated enable/disable endpoints. */
export function useSetConnectionEnabled(connectionId: string): UseMutationResult<
  Connection,
  Error,
  SetConnectionEnabledInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: connectionWriteMutationKey(connectionId),
    scope: { id: connectionMutationScopeId(connectionId) },
    mutationFn: async ({ connectionId, enabled }: SetConnectionEnabledInput) => {
      const path = enabled
        ? ('/v1/admin/connections/{connection_id}/enable' as const)
        : ('/v1/admin/connections/{connection_id}/disable' as const)
      const { data, error, response } = await getApexClient().POST(path, {
        params: { path: { connection_id: connectionId } },
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(
            error,
            `Connection ${enabled ? 'enable' : 'disable'} failed (${response.status})`,
          ),
          error,
        )
      }
      return data
    },
    onSuccess: (updated) => {
      if (isConnectionDeleted(queryClient, updated.id)) return
      queryClient.setQueryData(queryKeys.admin.connection(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.admin.connections() })
    },
  })
}

export interface PutHostMappingsInput {
  connectionId: string
  mappings: HostMappingIn[]
}

/** PUT semantics — replaces the FULL mapping list on save. */
export function usePutHostMappings(connectionId: string): UseMutationResult<
  HostMappingOut[],
  Error,
  PutHostMappingsInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: connectionWriteMutationKey(connectionId),
    scope: { id: connectionMutationScopeId(connectionId) },
    mutationFn: async ({ connectionId, mappings }: PutHostMappingsInput) => {
      const { data, error, response } = await getApexClient().PUT(
        '/v1/admin/connections/{connection_id}/host-mappings',
        { params: { path: { connection_id: connectionId } }, body: mappings },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Host mappings save failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (saved, { connectionId }) => {
      if (isConnectionDeleted(queryClient, connectionId)) return
      queryClient.setQueryData(queryKeys.admin.connectionHostMappings(connectionId), saved)
    },
  })
}

/**
 * Builds the adapter exactly as the resolver would and runs the kind's probe.
 * Always 200 — bad secret_ref / unreachable backend come back as ok=false and
 * the detail renders inline (never a toast, never a thrown error).
 */
export function useTestConnection(
  connectionId: string,
): UseMutationResult<ProbeResult, Error, void> {
  return useMutation({
    mutationKey: connectionProbeMutationKey(connectionId),
    scope: { id: connectionMutationScopeId(connectionId) },
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/admin/connections/{connection_id}/test',
        { params: { path: { connection_id: connectionId } } },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Connection test failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}
