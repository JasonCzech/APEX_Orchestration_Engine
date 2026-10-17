import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '@apex/api-client'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'

export type Cycle = components['schemas']['CycleRead']
export type Version = components['schemas']['CycleVersionRead']
export type VersionWrite = components['schemas']['VersionCreate']
export type Review = components['schemas']['CycleAction']
export type Field = components['schemas']['DeliverableField']

function unwrap<T>({
  data,
  error,
  response,
}: {
  data?: T
  error?: unknown
  response: Response
}): T {
  if (!response.ok || !data)
    throw new ApiError(
      response.status,
      errorMessageOf(error, response.status === 409 ? 'The cycle changed. Refresh its latest state before retrying this action.' : `Cycle request failed (${response.status})`),
      error,
    )
  return data
}
export function useCycles(offset: number, enabled: boolean, limit = 20) {
  return useQuery({
    queryKey: ['cycles', 'list', offset, limit],
    enabled,
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles', {
          params: { query: { offset, limit } },
          signal,
        }),
      ),
  })
}
export function useCycle(id: string, enabled: boolean) {
  return useQuery({
    queryKey: ['cycles', id],
    enabled: enabled && Boolean(id),
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles/{cycle_id}', {
          params: { path: { cycle_id: id } },
          signal,
        }),
      ),
  })
}
export function useCreateCycle() {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['CycleCreate']) =>
      unwrap(await getApexClient().POST('/v1/cycles', { body })),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}
export function useVersionWrite(id: string) {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: VersionWrite) =>
      unwrap(
        await getApexClient().POST('/v1/cycles/{cycle_id}/versions', {
          params: { path: { cycle_id: id } },
          body,
        }),
      ),
    onSuccess: (data) => {
      client.setQueryData(['cycles', id], data)
      void client.invalidateQueries({ queryKey: ['cycles', 'list'] })
    },
  })
}
export function useReview(id: string) {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: Review) =>
      unwrap(
        await getApexClient().POST('/v1/cycles/{cycle_id}/actions', {
          params: { path: { cycle_id: id } },
          body,
        }),
      ),
    onSuccess: (data) => {
      client.setQueryData(['cycles', id], data)
      void client.invalidateQueries({ queryKey: ['cycles', 'list'] })
    },
  })
}

export type Preparation = components['schemas']['PrepareExecution']
export function useExecutions(id: string, enabled = true) {
  return useQuery({
    queryKey: ['cycles', id, 'executions'],
    enabled,
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles/{cycle_id}/executions', {
          params: { path: { cycle_id: id } },
          signal,
        }),
      ),
  })
}
export function usePrepareExecution(id: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: Preparation) =>
      unwrap(
        await getApexClient().POST('/v1/cycles/{cycle_id}/executions', {
          params: { path: { cycle_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}
export function useArchivePreparation(id: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async ({ preparationId, ...body }: components['schemas']['ArchiveExecutionPreparation'] & { preparationId: string }) =>
      unwrap(await getApexClient().POST('/v1/cycles/{cycle_id}/executions/{preparation_id}/archive', {
        params: { path: { cycle_id: id, preparation_id: preparationId } }, body,
      })),
    onSettled: () => {
      void cache.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}

export function useLinkRun(id: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['LinkCycleRun']) =>
      unwrap(
        await getApexClient().POST('/v1/cycles/{cycle_id}/runs', {
          params: { path: { cycle_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}

export function useScriptLibrary(id: string) {
  return useQuery({
    queryKey: ['cycles', id, 'scripts'],
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles/{cycle_id}/scripts', {
          params: { path: { cycle_id: id } },
          signal,
        }),
      ),
  })
}
export function useStoreScript(id: string, versionId: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['StoreCycleScript']) =>
      unwrap(
        await getApexClient().POST(
          '/v1/cycles/{cycle_id}/scripts/{version_id}/artifact',
          { params: { path: { cycle_id: id, version_id: versionId } }, body },
        ),
      ),
    onSettled: () => {
      void cache.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}
export async function downloadScript(id: string, versionId: string) {
  const result = await getApexClient().GET(
    '/v1/cycles/{cycle_id}/scripts/{version_id}/content',
    {
      params: { path: { cycle_id: id, version_id: versionId } },
      parseAs: 'blob',
    },
  )
  if (!result.response.ok || !(result.data instanceof Blob))
    throw new Error(
      'Stored script could not be downloaded and verified. Recheck storage availability.',
    )
  return result.data
}

export function useScriptValidations(id: string, preparationId: string) {
  return useQuery({
    queryKey: ['cycles', id, 'validations', preparationId],
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET(
          '/v1/cycles/{cycle_id}/executions/{preparation_id}/script-validations',
          {
            params: { path: { cycle_id: id, preparation_id: preparationId } },
            signal,
          },
        ),
      ),
  })
}
export function useValidateScript(id: string, preparationId: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['ValidateCycleScript']) =>
      unwrap(
        await getApexClient().POST(
          '/v1/cycles/{cycle_id}/executions/{preparation_id}/script-validations',
          {
            params: { path: { cycle_id: id, preparation_id: preparationId } },
            body,
          },
        ),
      ),
    onSettled: () => {
      void cache.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}

export type ProviderImport = components['schemas']['ImportProviderScript']
export function useProviderScripts(
  id: string,
  connection: string,
  remoteId: string,
  offset: number,
  enabled: boolean,
  limit = 20,
) {
  return useQuery({
    queryKey: ['cycles', id, 'provider-scripts', connection, remoteId, offset, limit],
    enabled,
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles/{cycle_id}/provider-scripts', {
          params: {
            path: { cycle_id: id },
            query: {
              connection_id: connection || undefined,
              remote_script_id: remoteId || undefined,
              offset,
              limit,
            },
          },
          signal,
        }),
      ),
  })
}
export function useProviderImport(id: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: ProviderImport) =>
      unwrap(
        await getApexClient().POST(
          '/v1/cycles/{cycle_id}/provider-scripts/import',
          { params: { path: { cycle_id: id } }, body },
        ),
      ),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}

export function useCycleResults(id: string, baseline: string) {
  return useQuery({
    queryKey: ['cycles', id, 'results', baseline],
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles/{cycle_id}/results', {
          params: {
            path: { cycle_id: id },
            query: { baseline_id: baseline || undefined },
          },
          signal,
        }),
      ),
  })
}

export function useFreezeReportEvidence(id: string, versionId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['FreezeReportEvidence']) =>
      unwrap(
        await getApexClient().POST(
          '/v1/cycles/{cycle_id}/reports/{version_id}/evidence',
          {
            params: { path: { cycle_id: id, version_id: versionId } },
            body,
          },
        ),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['cycles', id] })
    },
  })
}

export function useCycleFollowup(id: string) {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['FollowupWrite']) =>
      unwrap(
        await getApexClient().POST('/v1/cycles/{cycle_id}/followups', {
          params: { path: { cycle_id: id } },
          body,
        }),
      ),
    onSuccess: (data) => {
      client.setQueryData(['cycles', id], data)
      void client.invalidateQueries({ queryKey: ['cycles', id] })
    },
  })
}
export function useNextCycle(id: string) {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['NextCycleRequest']) =>
      unwrap(
        await getApexClient().POST('/v1/cycles/{cycle_id}/next-cycle', {
          params: { path: { cycle_id: id } },
          body,
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['cycles'] })
    },
  })
}

export function useCycleSources(
  id: string,
  connection: string,
  title: string,
  page: string,
  revision: string,
  enabled: boolean,
) {
  return useQuery({
    queryKey: ['cycles', id, 'sources', connection, title, page, revision],
    enabled,
    queryFn: async ({ signal }) =>
      unwrap(
        await getApexClient().GET('/v1/cycles/{cycle_id}/sources', {
          params: {
            path: { cycle_id: id },
            query: {
              connection_id: connection || undefined,
              title,
              page_id: page || undefined,
              revision: revision || undefined,
            },
          },
          signal,
        }),
      ),
  })
}
export function useCaptureCycleSource(id: string, versionId: string) {
  const client = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['CaptureSource']) =>
      unwrap(
        await getApexClient().POST(
          '/v1/cycles/{cycle_id}/scopes/{version_id}/sources',
          { params: { path: { cycle_id: id, version_id: versionId } }, body },
        ),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['cycles', id] })
    },
  })
}
