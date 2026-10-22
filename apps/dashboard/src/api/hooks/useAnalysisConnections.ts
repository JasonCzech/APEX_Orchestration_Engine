import { useQuery } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
export function useAnalysisConnections(projectId: string, enabled = true) {
  return useQuery({ queryKey: ['analysis-connections', projectId], enabled: enabled && Boolean(projectId), queryFn: async () => {
    const { data, response } = await getApexClient().GET('/v1/pipelines/analysis/connections', { params: { query: { project_id: projectId } } })
    if (!response.ok || !data) throw new Error('Unable to load analysis connectors')
    return data
  } })
}
