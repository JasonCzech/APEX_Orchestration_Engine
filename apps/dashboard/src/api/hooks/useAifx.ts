/**
 * AIFx Context Services over /v1/admin/aifx (route /admin/aifx) and the run's
 * pinned content over /v1/pipelines/{thread_id}/aifx.
 *
 * Organizations name the GitHub Enterprise connection, owner and global-rules
 * repository; application mappings add an APM ID, team-documents and ACB
 * repository. Mutations invalidate the ['admin', 'aifx'] prefix. Preview is a
 * cache-free dry run of what a launch would pin; problems render inline.
 */
import { useMutation, useQuery, useQueryClient, type UseMutationResult, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type AifxOrganization = components['schemas']['OrganizationOut']
export type AifxOrganizationWrite = components['schemas']['OrganizationWrite']
export type AifxOrganizationUpdate = components['schemas']['OrganizationUpdate']
export type AifxApplicationSource = components['schemas']['ApplicationSourceOut']
export type AifxApplicationSourceWrite = components['schemas']['ApplicationSourceWrite']
export type AifxSuggestion = components['schemas']['ApplicationSourceSuggestion']
export type AifxPreview = components['schemas']['AifxPreview']
export type PipelineAifx = components['schemas']['PipelineAifx']
export type AifxSnapshot = components['schemas']['AifxSnapshotV1']

function fail(response: Response, error: unknown, fallback: string): never {
  throw new ApiError(response.status, errorMessageOf(error, `${fallback} (${response.status})`), error)
}

export function useAifxOrganizations(): UseQueryResult<AifxOrganization[], Error> {
  return useQuery({
    queryKey: queryKeys.admin.aifxOrganizations(),
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/admin/aifx/organizations', { signal })
      if (!response.ok || !data) fail(response, error, 'AIFx organizations failed to load')
      return data
    },
    staleTime: STALE_TIMES.admin,
  })
}

export function useAifxSources(): UseQueryResult<AifxApplicationSource[], Error> {
  return useQuery({
    queryKey: queryKeys.admin.aifxSources(),
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/admin/aifx/applications', { signal })
      if (!response.ok || !data) fail(response, error, 'AIFx applications failed to load')
      return data
    },
    staleTime: STALE_TIMES.admin,
  })
}

export function useCreateAifxOrganization(): UseMutationResult<AifxOrganization, Error, AifxOrganizationWrite> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body) => {
      const { data, error, response } = await getApexClient().POST('/v1/admin/aifx/organizations', { body })
      if (!response.ok || !data) fail(response, error, 'Organization onboarding failed')
      return data
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.admin.aifx() }),
  })
}

export function useUpdateAifxOrganization(): UseMutationResult<
  AifxOrganization,
  Error,
  { organizationId: string; body: AifxOrganizationUpdate }
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ organizationId, body }) => {
      const { data, error, response } = await getApexClient().PATCH('/v1/admin/aifx/organizations/{organization_id}', {
        params: { path: { organization_id: organizationId } },
        body,
      })
      if (!response.ok || !data) fail(response, error, 'Organization update failed')
      return data
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.admin.aifx() }),
  })
}

export function useDeleteAifxOrganization(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (organizationId) => {
      const { error, response } = await getApexClient().DELETE('/v1/admin/aifx/organizations/{organization_id}', {
        params: { path: { organization_id: organizationId } },
      })
      if (!response.ok) fail(response, error, 'Organization removal failed')
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.admin.aifx() }),
  })
}

export async function fetchAifxSuggestion(
  applicationId: string,
  organizationId: string,
  apmId: string,
): Promise<AifxSuggestion> {
  const { data, error, response } = await getApexClient().GET('/v1/admin/aifx/applications/{application_id}/suggest', {
    params: { path: { application_id: applicationId }, query: { organization_id: organizationId, apm_id: apmId } },
  })
  if (!response.ok || !data) fail(response, error, 'Repository suggestion failed')
  return data
}

export function usePutAifxSource(): UseMutationResult<
  AifxApplicationSource,
  Error,
  { applicationId: string; body: AifxApplicationSourceWrite }
> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async ({ applicationId, body }) => {
      const { data, error, response } = await getApexClient().PUT('/v1/admin/aifx/applications/{application_id}', {
        params: { path: { application_id: applicationId } },
        body,
      })
      if (!response.ok || !data) fail(response, error, 'Application onboarding failed')
      return data
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.admin.aifx() }),
  })
}

export function useDeleteAifxSource(): UseMutationResult<void, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (applicationId) => {
      const { error, response } = await getApexClient().DELETE('/v1/admin/aifx/applications/{application_id}', {
        params: { path: { application_id: applicationId } },
      })
      if (!response.ok) fail(response, error, 'Application offboarding failed')
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: queryKeys.admin.aifx() }),
  })
}

/** Dry run. Pass a draft to check unsaved values; omit it to preview the saved mapping. */
export function usePreviewAifxSource(): UseMutationResult<
  AifxPreview,
  Error,
  { applicationId: string; draft?: AifxApplicationSourceWrite }
> {
  return useMutation({
    mutationFn: async ({ applicationId, draft }) => {
      const { data, error, response } = await getApexClient().POST('/v1/admin/aifx/applications/{application_id}/preview', {
        params: { path: { application_id: applicationId } },
        ...(draft ? { body: draft } : {}),
      })
      if (!response.ok || !data) fail(response, error, 'Preview failed')
      return data
    },
  })
}

/** The run's pinned AIFx content. It never changes after launch, so it is cached for good. */
export function usePipelineAifx(threadId: string | undefined, enabled = true): UseQueryResult<PipelineAifx, Error> {
  return useQuery({
    queryKey: queryKeys.threads.aifx(threadId ?? ''),
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/pipelines/{thread_id}/aifx', {
        params: { path: { thread_id: threadId ?? '' } },
        signal,
      })
      if (!response.ok || !data) fail(response, error, 'Pinned AIFx context failed to load')
      return data
    },
    enabled: Boolean(threadId) && enabled,
    staleTime: Infinity,
  })
}
