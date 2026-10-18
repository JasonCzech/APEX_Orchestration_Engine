import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import type { components } from '@apex/api-client'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'

export type DispatchWorkspace = components['schemas']['CycleDispatchWorkspace']
export type DispatchAttempt = components['schemas']['CycleDispatchRead']

const active = new Set(['reserved', 'submitted', 'uncertain', 'running', 'cleanup_pending'])

function unwrap<T>({ data, error, response }: { data?: T; error?: unknown; response: Response }): T {
  if (!response.ok || !data) throw new ApiError(response.status,
    errorMessageOf(error, 'Execution dispatch could not be confirmed. Refresh its receipt before retrying.'), error)
  return data
}

export function useExecutionDispatch(cycleId: string, preparationId: string) {
  return useQuery({
    queryKey: ['cycles', cycleId, 'dispatch', preparationId],
    queryFn: async ({ signal }) => unwrap(await getApexClient().GET(
      '/v1/cycles/{cycle_id}/executions/{preparation_id}/dispatch',
      { params: { path: { cycle_id: cycleId, preparation_id: preparationId } }, signal },
    )),
    refetchInterval: query => active.has(query.state.data?.attempt?.status ?? '') ? 3000 : false,
  })
}

export function useDispatchExecution(cycleId: string, preparationId: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: components['schemas']['DispatchCycleExecution']) => unwrap(await getApexClient().POST(
      '/v1/cycles/{cycle_id}/executions/{preparation_id}/dispatch',
      { params: { path: { cycle_id: cycleId, preparation_id: preparationId } }, body },
    )),
    onSuccess: data => { cache.setQueryData(['cycles', cycleId, 'dispatch', preparationId], data) },
    onSettled: () => { void cache.invalidateQueries({ queryKey: ['cycles'] }) },
  })
}

export function useCancelExecutionDispatch(cycleId: string, preparationId: string) {
  const cache = useQueryClient()
  return useMutation({
    retry: false,
    mutationFn: async (body: { dispatch_id: string }) => unwrap(await getApexClient().POST(
      '/v1/cycles/{cycle_id}/executions/{preparation_id}/dispatch/cancel',
      { params: { path: { cycle_id: cycleId, preparation_id: preparationId } }, body },
    )),
    onSuccess: data => { cache.setQueryData(['cycles', cycleId, 'dispatch', preparationId], data) },
    onSettled: () => { void cache.invalidateQueries({ queryKey: ['cycles'] }) },
  })
}
