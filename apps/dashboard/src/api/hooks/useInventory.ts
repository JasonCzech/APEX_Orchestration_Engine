/**
 * /v1/inventory — latest k8s cluster-inventory snapshot per environment plus
 * the on-demand synchronous rescan (plan route /environments/:id, D5).
 *
 * GET returns { environment_id, snapshot } where snapshot is null until the
 * environment has ever been scanned; snapshot.stale flags scans older than the
 * server's threshold. POST .../rescan runs the adapter INLINE and returns the
 * fresh payload — success writes it straight into the query cache (no refetch
 * race) and invalidates the catalog detail so its last_snapshot summary keeps
 * up. Adapter/resolution failures surface as a 502 problem whose detail is the
 * adapter message — callers render it inline (probe-style), not as a toast.
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
import {
  environmentWriteMutationKey,
  environmentWriteMutationScopeId,
} from '@/api/hooks/useEnvironments'
import { queryKeys } from '@/api/queryKeys'

export type InventoryView = components['schemas']['InventoryView']
export type SnapshotView = components['schemas']['SnapshotView']
export type ServiceInfo = components['schemas']['ServiceInfo']

async function fetchInventory(environmentId: string): Promise<InventoryView> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/inventory/environments/{environment_id}',
    { params: { path: { environment_id: environmentId } } },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Inventory request failed (${response.status})`),
      error,
    )
  }
  return data
}

/** Latest persisted snapshot for one environment (null snapshot = never scanned). */
export function useEnvironmentInventory(
  environmentId: string | undefined,
): UseQueryResult<InventoryView, Error> {
  return useQuery({
    queryKey: queryKeys.inventory.environment(environmentId ?? ''),
    queryFn: () => fetchInventory(environmentId ?? ''),
    enabled: Boolean(environmentId),
    staleTime: 30_000,
  })
}

/**
 * Synchronous rescan mutation. The scan happens inline server-side, so the
 * pending state doubles as the button's "Scanning…" indicator.
 */
export function useRescanEnvironment(
  environmentId: string,
): UseMutationResult<InventoryView, Error, void> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: environmentWriteMutationKey(environmentId),
    scope: { id: environmentWriteMutationScopeId(environmentId) },
    mutationFn: async () => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/inventory/environments/{environment_id}/rescan',
        { params: { path: { environment_id: environmentId } } },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Environment rescan failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (fresh) => {
      queryClient.setQueryData(queryKeys.inventory.environment(environmentId), fresh)
      // The catalog detail carries a last_snapshot summary — keep it honest.
      void queryClient.invalidateQueries({
        queryKey: queryKeys.catalog.environment(environmentId),
      })
    },
  })
}
