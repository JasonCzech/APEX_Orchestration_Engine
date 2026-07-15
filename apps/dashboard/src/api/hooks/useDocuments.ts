import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type DocumentOut = components['schemas']['DocumentOut']

const DOCUMENT_PAGE_SIZE = 200

async function fetchDocuments(project?: string, q?: string): Promise<DocumentOut[]> {
  const items: DocumentOut[] = []
  let offset = 0
  for (;;) {
    const { data, error, response } = await getApexClient().GET('/v1/documents', {
      params: {
        query: {
          ...(project ? { project } : {}),
          ...(q ? { q } : {}),
          limit: DOCUMENT_PAGE_SIZE,
          offset,
        },
      },
    })
    if (!response.ok || !data) {
      throw new ApiError(
        response.status,
        errorMessageOf(error, `Documents request failed (${response.status})`),
        error,
      )
    }
    items.push(...data.items)
    if (data.items.length < DOCUMENT_PAGE_SIZE) return items
    offset += data.items.length
  }
}

/**
 * Existing documents for the wizard Context step picker and the /context
 * Documents tab. D6 extension: optional `q` name search — keyed under the
 * D6 listWith key only when present so the wizard's cache entries are
 * untouched.
 */
export function useDocumentsList(
  project?: string,
  q?: string,
  appId?: string | null,
): UseQueryResult<DocumentOut[], Error> {
  return useQuery({
    queryKey: queryKeys.documents.listWith({
      project: project ?? null,
      q: q ?? null,
      app: appId === undefined ? 'all' : appId,
    }),
    queryFn: async () => {
      const documents = await fetchDocuments(project, q)
      if (appId === undefined) return documents
      return documents.filter((document) => !document.app_id || document.app_id === appId)
    },
    staleTime: 30_000,
  })
}

export interface UploadDocumentInput {
  file: File
  projectId?: string
  appId?: string
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
    mutationFn: async ({ file, projectId, appId, summary }) => {
      const { data, response } = await getApexClient().POST('/v1/documents', {
        body: { file: file as unknown as string },
        bodySerializer: () => {
          const form = new FormData()
          form.append('file', file, file.name)
          if (projectId) form.append('project_id', projectId)
          if (appId) form.append('app_id', appId)
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

/** D6 append: delete a document (operator+; DELETE /v1/documents/{document_id}). */
export function useDeleteDocument() {
  const queryClient = useQueryClient()
  return useMutation<void, Error, string>({
    mutationFn: async (documentId: string) => {
      const { error, response } = await getApexClient().DELETE('/v1/documents/{document_id}', {
        params: { path: { document_id: documentId } },
      })
      if (!response.ok) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Document delete failed (${response.status})`),
          error,
        )
      }
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.documents.all })
    },
  })
}
