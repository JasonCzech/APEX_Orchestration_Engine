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
  type QueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages } from '@/api/fetchAllPages'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type Application = components['schemas']['ApplicationOut']
export type ApplicationCreate = components['schemas']['ApplicationCreate']
export type Environment = components['schemas']['EnvironmentOut']
export type EnvironmentCreate = components['schemas']['EnvironmentCreate']
export type EnvironmentUpdate = components['schemas']['EnvironmentUpdate']
export type HostIn = components['schemas']['HostIn']
export type HostOut = components['schemas']['HostOut']
const CATALOG_PAGE_SIZE = 200
const deletedEnvironmentIds = new WeakMap<QueryClient, Set<string>>()

export function environmentWriteMutationKey(environmentId: string) {
  return ['catalog', 'environments', 'write', environmentId] as const
}

export function environmentWriteMutationScopeId(environmentId: string): string {
  return `catalog:environments:write:${environmentId}`
}

function deletedIdsFor(queryClient: QueryClient): Set<string> {
  const existing = deletedEnvironmentIds.get(queryClient)
  if (existing) return existing
  const created = new Set<string>()
  deletedEnvironmentIds.set(queryClient, created)
  return created
}

function isEnvironmentDeleted(queryClient: QueryClient, environmentId: string): boolean {
  return deletedEnvironmentIds.get(queryClient)?.has(environmentId) ?? false
}

async function fetchApplicationsIndex(signal?: AbortSignal, includeArchived = false): Promise<Application[]> {
  return fetchAllOffsetPages({
    label: 'Applications',
    pageSize: CATALOG_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/catalog/applications', {
        params: { query: { limit, offset, ...(includeArchived ? { include_archived: true } : {}) } },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Applications request failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

async function fetchEnvironmentsIndex(signal?: AbortSignal): Promise<Environment[]> {
  return fetchAllOffsetPages({
    label: 'Environments',
    pageSize: CATALOG_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/catalog/environments', {
        params: { query: { limit, offset } },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Environments request failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
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
export function useApplicationsIndex(options: { includeArchived?: boolean } = {}): UseQueryResult<Application[], Error> {
  const includeArchived = options.includeArchived === true
  return useQuery({
    queryKey: includeArchived ? [...queryKeys.catalog.applicationsIndex(), 'with-archived'] : queryKeys.catalog.applicationsIndex(),
    queryFn: ({ signal }) => fetchApplicationsIndex(signal, includeArchived),
    staleTime: STALE_TIMES.catalog,
  })
}

/** Rename or describe an application (PATCH; operator+ with access to it). */
export function useUpdateApplication(): UseMutationResult<Application, Error, { applicationId: string; body: { name?: string; description?: string | null; project_ids?: string[] } }> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ applicationId, body }) => {
      const { data, error, response } = await getApexClient().PATCH('/v1/catalog/applications/{application_id}', {
        params: { path: { application_id: applicationId } },
        body,
      })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, `Application update failed (${response.status})`), error)
      return data
    },
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.all }) },
  })
}

/** Archive hides an application from new tests; unarchive restores it. */
export function useSetApplicationArchived(): UseMutationResult<Application, Error, { applicationId: string; archived: boolean }> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ applicationId, archived }) => {
      const { data, error, response } = archived
        ? await getApexClient().POST('/v1/catalog/applications/{application_id}/archive', { params: { path: { application_id: applicationId } } })
        : await getApexClient().POST('/v1/catalog/applications/{application_id}/unarchive', { params: { path: { application_id: applicationId } } })
      if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, `Application ${archived ? 'archive' : 'unarchive'} failed (${response.status})`), error)
      return data
    },
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.all }) },
  })
}

/** Delete an application and its environments (global admin; refused while referenced). */
export function useDeleteApplication(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (applicationId: string) => {
      const { error, response } = await getApexClient().DELETE('/v1/catalog/applications/{application_id}', {
        params: { path: { application_id: applicationId } },
      })
      if (!response.ok) throw new ApiError(response.status, errorMessageOf(error, `Application delete failed (${response.status})`), error)
    },
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.all }) },
  })
}

export function useCreateApplication(): UseMutationResult<Application, Error, ApplicationCreate> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: ApplicationCreate) => {
      const { data, error, response } = await getApexClient().POST('/v1/catalog/applications', {
        body,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Application create failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (created) => {
      queryClient.setQueryData<Application[]>(
        queryKeys.catalog.applicationsIndex(),
        (current) => current ? [...current.filter((application) => application.id !== created.id), created] : [created],
      )
      void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.applications() })
    },
  })
}

/** All visible environments (no application filter) for the /environments list. */
export function useEnvironmentsIndex(): UseQueryResult<Environment[], Error> {
  return useQuery({
    queryKey: queryKeys.catalog.environmentsIndex(),
    queryFn: ({ signal }) => fetchEnvironmentsIndex(signal),
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
      deletedIdsFor(queryClient).delete(created.id)
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

export function useUpdateEnvironment(environmentId: string): UseMutationResult<
  Environment,
  Error,
  UpdateEnvironmentInput
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: environmentWriteMutationKey(environmentId),
    scope: { id: environmentWriteMutationScopeId(environmentId) },
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
      if (isEnvironmentDeleted(queryClient, updated.id)) return
      queryClient.setQueryData(queryKeys.catalog.environment(updated.id), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.environments() })
    },
  })
}

export function useDeleteEnvironment(environmentId: string): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: environmentWriteMutationKey(environmentId),
    scope: { id: environmentWriteMutationScopeId(environmentId) },
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
      deletedIdsFor(queryClient).add(environmentId)
      queryClient.removeQueries({ queryKey: queryKeys.catalog.environment(environmentId) })
      queryClient.removeQueries({ queryKey: queryKeys.inventory.environment(environmentId) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.catalog.environments() })
    },
  })
}
