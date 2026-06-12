/**
 * Environment-reference CRUD over /v1/catalog (plan route /environments).
 *
 * Distinct from useCatalog.ts: that module serves the wizard's scoped pickers
 * (?project= / ?application= filtered, disabled until upstream input exists),
 * while these hooks fetch the UNFILTERED indexes the environments screens
 * group client-side, plus the create/update/delete mutations.
 *
 * Every mutation invalidates the ['catalog', 'environments'] prefix, which
 * fans out to the index, the wizard's by-application lists, and any cached
 * environment details in one shot.
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
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type Application = components['schemas']['ApplicationOut']
export type Environment = components['schemas']['EnvironmentOut']
export type EnvironmentCreate = components['schemas']['EnvironmentCreate']
export type EnvironmentUpdate = components['schemas']['EnvironmentUpdate']
export type HostIn = components['schemas']['HostIn']
export type HostOut = components['schemas']['HostOut']

async function fetchApplicationsIndex(): Promise<Application[]> {
  const { data, error, response } = await getApexClient().GET('/v1/catalog/applications', {})
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Applications request failed (${response.status})`),
      error,
    )
  }
  return data
}

async function fetchEnvironmentsIndex(): Promise<Environment[]> {
  const { data, error, response } = await getApexClient().GET('/v1/catalog/environments', {})
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Environments request failed (${response.status})`),
      error,
    )
  }
  return data
}

async function fetchEnvironment(environmentId: string): Promise<Environment> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/catalog/environments/{environment_id}',
    { params: { path: { environment_id: environmentId } } },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Environment request failed (${response.status})`),
      error,
    )
  }
  return data
}

/** All visible applications — the /environments list groups rows under these. */
export function useApplicationsIndex(): UseQueryResult<Application[], Error> {
  return useQuery({
    queryKey: queryKeys.catalog.applicationsIndex(),
    queryFn: fetchApplicationsIndex,
    staleTime: STALE_TIMES.catalog,
  })
}

/** All visible environments (no application filter) for the /environments list. */
export function useEnvironmentsIndex(): UseQueryResult<Environment[], Error> {
  return useQuery({
    queryKey: queryKeys.catalog.environmentsIndex(),
    queryFn: fetchEnvironmentsIndex,
    staleTime: STALE_TIMES.catalog,
  })
}

/** One environment (detail view; includes hosts, options and last_snapshot summary). */
export function useEnvironment(
  environmentId: string | undefined,
): UseQueryResult<Environment, Error> {
  return useQuery({
    queryKey: queryKeys.catalog.environment(environmentId ?? ''),
    queryFn: () => fetchEnvironment(environmentId ?? ''),
    enabled: Boolean(environmentId),
    staleTime: STALE_TIMES.catalog,
  })
}

export function useCreateEnvironment(): UseMutationResult<Environment, Error, EnvironmentCreate> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: EnvironmentCreate) => {
      const { data, error, response } = await getApexClient().POST('/v1/catalog/environments', {
        body,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Environment create failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (created) => {
      // Seed the detail cache so the post-create navigation paints instantly.
      queryClient.setQueryData(queryKeys.catalog.environment(created.id), created)
      void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.environments() })
    },
  })
}

export interface UpdateEnvironmentInput {
  environmentId: string
  body: EnvironmentUpdate
}

export function useUpdateEnvironment(): UseMutationResult<
  Environment,
  Error,
  UpdateEnvironmentInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ environmentId, body }: UpdateEnvironmentInput) => {
      const { data, error, response } = await getApexClient().PATCH(
        '/v1/catalog/environments/{environment_id}',
        { params: { path: { environment_id: environmentId } }, body },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Environment update failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.catalog.environment(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.environments() })
    },
  })
}

export function useDeleteEnvironment(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (environmentId: string) => {
      const { error, response } = await getApexClient().DELETE(
        '/v1/catalog/environments/{environment_id}',
        { params: { path: { environment_id: environmentId } } },
      )
      if (!response.ok) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Environment delete failed (${response.status})`),
          error,
        )
      }
    },
    onSuccess: (_void, environmentId) => {
      queryClient.removeQueries({ queryKey: queryKeys.catalog.environment(environmentId) })
      queryClient.removeQueries({ queryKey: queryKeys.inventory.environment(environmentId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.environments() })
    },
  })
}
