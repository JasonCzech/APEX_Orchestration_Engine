import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages } from '@/api/fetchAllPages'

export function useProjectLabels() {
  return useQuery({
    queryKey: ['catalog', 'project-labels'], retry: false,
    queryFn: ({ signal }) => fetchAllOffsetPages({ label: 'Project names', pageSize: 200,
      fetchPage: async (limit, offset) => {
        const { data, error, response } = await getApexClient().GET('/v1/catalog/projects', { params: { query: { limit, offset } }, signal })
        if (!response.ok || !data) throw new ApiError(response.status, errorMessageOf(error, 'Project names are unavailable'), error)
        return data
      },
    }),
  })
}

export function useProjectLabelWrite(projectId: string) {
  const cache = useQueryClient()
  return useMutation({
    mutationKey: ['catalog', 'project-labels', projectId], scope: { id: `project-name:${projectId}` }, retry: false,
    mutationFn: async (name: string | null) => {
      const result = name === null
        ? await getApexClient().DELETE('/v1/catalog/projects', { params: { query: { project: projectId } } })
        : await getApexClient().PUT('/v1/catalog/projects', { body: { project_id: projectId, name } })
      if (!result.response.ok) throw new ApiError(result.response.status, errorMessageOf(result.error, 'Project name could not be saved'), result.error)
    },
    onSuccess: () => cache.invalidateQueries({ queryKey: ['catalog', 'project-labels'] }),
  })
}
