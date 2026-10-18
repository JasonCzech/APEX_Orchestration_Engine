import { useQuery } from '@tanstack/react-query'
import { z } from 'zod'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

const readinessSchema = z.object({
  execution_selected: z.boolean(),
  configuration_valid: z.boolean(),
  requires_application: z.boolean(),
  detail: z.string().nullable(),
})

export type ExecutionConfigurationReadiness = z.infer<typeof readinessSchema>
export interface ExecutionReadinessRequest {
  project_id: string
  app_id?: string
  engine: string
  connection_id?: string
  artifact_connection_id?: string
  phases: string[]
}

/** Checks scoped connection metadata only; this never probes a provider. */
export function useExecutionReadiness(request: ExecutionReadinessRequest, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.pipelines.executionReadiness(request),
    enabled,
    staleTime: 0,
    retry: false,
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().POST('/v1/pipelines/execution-readiness', { body: request, signal })
      if (!response.ok) throw new ApiError(response.status, errorMessageOf(error, 'Execution configuration could not be verified'), error)
      const parsed = readinessSchema.safeParse(data)
      if (!parsed.success || !parsed.data.execution_selected) throw new Error('Execution configuration response was incomplete; retry verification')
      return parsed.data
    },
  })
}
