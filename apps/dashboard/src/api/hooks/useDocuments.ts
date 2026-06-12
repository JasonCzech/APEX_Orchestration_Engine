import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type DocumentOut = components['schemas']['DocumentOut']

async function fetchDocuments(project?: string): Promise<DocumentOut[]> {
  const { data, error, response } = await getApexClient().GET('/v1/documents', {
    params: { query: project ? { project } : {} },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Documents request failed (${response.status})`),
      error,
    )
  }
  return data.items
}

/** Existing documents for the wizard Context step picker. */
export function useDocumentsList(project?: string): UseQueryResult<DocumentOut[], Error> {
  return useQuery({
    queryKey: queryKeys.documents.listBy(project),
    queryFn: () => fetchDocuments(project),
    staleTime: 30_000,
  })
}

export interface UploadDocumentInput {
  file: File
  projectId?: string
  summary?: string
}

/**
 * Multipart upload (POST /v1/documents). The generated schema types `file` as
 * a binary string; the bodySerializer swaps in real FormData so the browser
 * sets the multipart boundary (openapi-fetch leaves Content-Type unset).
 */
export function useUploadDocument() {
  const queryClient = useQueryClient()
  return useMutation<DocumentOut, Error, UploadDocumentInput>({
    mutationFn: async ({ file, projectId, summary }) => {
      const { data, response } = await getApexClient().POST('/v1/documents', {
        body: { file: file as unknown as string },
        bodySerializer: () => {
          const form = new FormData()
          form.append('file', file, file.name)
          if (projectId) form.append('project_id', projectId)
          if (summary) form.append('summary', summary)
          return form
        },
      })
      if (!response.ok || !data) {
        // Spec declares only 201 for this op, so the typed error branch is never.
        throw new ApiError(response.status, `Upload of ${file.name} failed (${response.status})`)
      }
      return data
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents.all })
    },
  })
}
