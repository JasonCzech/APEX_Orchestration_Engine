import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type Application = components['schemas']['ApplicationOut']
export type Environment = components['schemas']['EnvironmentOut']

async function fetchApplications(project: string): Promise<Application[]> {
  const { data, error, response } = await getApexClient().GET('/v1/catalog/applications', {
    params: { query: { project } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Applications request failed (${response.status})`),
      error,
    )
  }
  return data
}

async function fetchEnvironments(application: string): Promise<Environment[]> {
  const { data, error, response } = await getApexClient().GET('/v1/catalog/environments', {
    params: { query: { application } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Environments request failed (${response.status})`),
      error,
    )
  }
  return data
}

/** Applications for one project (wizard Scope step). Disabled until a project is typed. */
export function useApplications(project: string | undefined): UseQueryResult<Application[], Error> {
  const trimmed = project?.trim() ?? ''
  return useQuery({
    queryKey: queryKeys.catalog.applicationsBy(trimmed || undefined),
    queryFn: () => fetchApplications(trimmed),
    enabled: trimmed.length > 0,
    staleTime: STALE_TIMES.catalog,
  })
}

/** Environments for one application (wizard Scope step). Disabled until an app is picked. */
export function useEnvironments(
  application: string | null | undefined,
): UseQueryResult<Environment[], Error> {
  return useQuery({
    queryKey: queryKeys.catalog.environmentsBy(application ?? null),
    queryFn: () => fetchEnvironments(application ?? ''),
    enabled: Boolean(application),
    staleTime: STALE_TIMES.catalog,
  })
}
