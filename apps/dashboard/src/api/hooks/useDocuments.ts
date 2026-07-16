import {
  hashKey,
  useMutation,
  useQuery,
  useQueryClient,
  type QueryClient,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages } from '@/api/fetchAllPages'
import { queryKeys } from '@/api/queryKeys'
import { getApiKeyRevision, getSessionRevision } from '@/auth/keyStorage'

export type DocumentOut = components['schemas']['DocumentOut']

const DOCUMENT_PAGE_SIZE = 200

export function documentsListQueryKey(
  project?: string,
  q?: string,
  appId?: string | null,
) {
  return queryKeys.documents.listWith({
    project: project ?? null,
    q: q ?? null,
    appScope: appId === undefined ? 'all' : 'selected',
    app: appId ?? null,
  })
}

async function fetchDocuments(
  project?: string,
  q?: string,
  signal?: AbortSignal,
): Promise<DocumentOut[]> {
  return fetchAllOffsetPages({
    label: 'Documents',
    pageSize: DOCUMENT_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/documents', {
        params: {
          query: {
            ...(project ? { project } : {}),
            ...(q ? { q } : {}),
            limit,
            offset,
          },
        },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Documents request failed (${response.status})`),
          error,
        )
      }
      return data.items
    },
  })
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
    queryKey: documentsListQueryKey(project, q, appId),
    queryFn: async ({ signal }) => {
      const documents = await fetchDocuments(project, q, signal)
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

interface DocumentUploadIdentity {
  projectId: string | null
  appId: string | null
  summary: string | null
  file: {
    name: string
    size: number
    type: string
    lastModified: number
    relativePath: string | null
  }
}

export function documentUploadIdentity(input: UploadDocumentInput): DocumentUploadIdentity {
  return {
    projectId: input.projectId ?? null,
    appId: input.appId ?? null,
    summary: input.summary ?? null,
    file: {
      name: input.file.name,
      size: input.file.size,
      type: input.file.type,
      lastModified: input.file.lastModified,
      relativePath: input.file.webkitRelativePath || null,
    },
  }
}

export function documentUploadMutationKey(input: UploadDocumentInput) {
  return ['documents', 'upload', documentUploadIdentity(input)] as const
}

export function documentUploadMutationScopeId(input: UploadDocumentInput): string {
  return `document-upload:${hashKey(documentUploadMutationKey(input))}`
}

export function documentUploadBatchMutationKey() {
  return ['documents', 'upload-batch'] as const
}

export class DocumentUploadInFlightError extends Error {
  constructor(fileName: string) {
    super(`An identical upload of ${fileName} is already in progress.`)
    this.name = 'DocumentUploadInFlightError'
  }
}

async function uploadDocument({
  file,
  projectId,
  appId,
  summary,
}: UploadDocumentInput): Promise<DocumentOut> {
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
}

async function executeIdentifiedDocumentUpload(
  queryClient: QueryClient,
  input: UploadDocumentInput,
): Promise<DocumentOut> {
  const mutationKey = documentUploadMutationKey(input)
  const mutationCache = queryClient.getMutationCache()
  const existing = mutationCache.find({
    exact: true,
    mutationKey,
    status: 'pending',
  })
  if (existing) throw new DocumentUploadInFlightError(input.file.name)

  const mutation = mutationCache.build<DocumentOut, Error, UploadDocumentInput, unknown>(
    queryClient,
    {
      mutationKey,
      scope: { id: documentUploadMutationScopeId(input) },
      mutationFn: uploadDocument,
      onSuccess: () => {
        void queryClient.invalidateQueries({ queryKey: queryKeys.documents.all })
      },
    },
  )
  return mutation.execute(input)
}

/**
 * Multipart upload (POST /v1/documents). The generated schema types `file` as
 * a binary string; the bodySerializer swaps in real FormData so the browser
 * sets the multipart boundary (openapi-fetch leaves Content-Type unset).
 */
export function useUploadDocument() {
  const queryClient = useQueryClient()
  return useMutation<DocumentOut, Error, UploadDocumentInput>({
    mutationFn: (input) => executeIdentifiedDocumentUpload(queryClient, input),
  })
}

export interface UploadDocumentBatchInput {
  files: File[]
  projectId?: string
  appId?: string
  /** Committed list search preserved solely for remount-safe UI restoration. */
  q?: string
}

export interface UploadDocumentBatchResult {
  uploaded: DocumentOut[]
  errors: Error[]
  sessionChanged: boolean
}

export interface DocumentUploadBatchOutcome {
  uploadedCount: number
  errors: string[]
  completedAt: string
  projectId?: string
  appId?: string
  q?: string
}

/**
 * The latest actionable batch failure is client-owned session state. Keeping
 * it in the query cache lets a mutation publish after its tab observer
 * unmounts, while the auth lifecycle still discards it on principal changes.
 */
export function useDocumentUploadBatchOutcome(): UseQueryResult<
  DocumentUploadBatchOutcome | null,
  Error
> {
  return useQuery({
    queryKey: queryKeys.documents.uploadOutcome(),
    queryFn: async (): Promise<DocumentUploadBatchOutcome | null> => null,
    initialData: null,
    enabled: false,
    staleTime: Infinity,
    gcTime: Infinity,
  })
}

/** Context-tab batch wrapper whose pending identity survives tab remounts. */
export function useUploadDocumentBatch() {
  const queryClient = useQueryClient()
  return useMutation<UploadDocumentBatchResult, Error, UploadDocumentBatchInput>({
    mutationKey: documentUploadBatchMutationKey(),
    onMutate: () => {
      queryClient.setQueryData<DocumentUploadBatchOutcome | null>(
        queryKeys.documents.uploadOutcome(),
        null,
      )
    },
    mutationFn: async ({ files, projectId, appId }) => {
      const keyRevision = getApiKeyRevision()
      const sessionRevision = getSessionRevision()
      const belongsToCurrentSession = () =>
        keyRevision === getApiKeyRevision() && sessionRevision === getSessionRevision()
      const uploaded: DocumentOut[] = []
      const errors: Error[] = []
      let sessionChanged = false

      for (const file of files) {
        if (!belongsToCurrentSession()) {
          sessionChanged = true
          break
        }
        try {
          uploaded.push(
            await executeIdentifiedDocumentUpload(queryClient, {
              file,
              projectId,
              appId,
            }),
          )
        } catch (error) {
          if (!belongsToCurrentSession()) {
            sessionChanged = true
            break
          }
          errors.push(
            error instanceof Error ? error : new Error(`Upload of ${file.name} failed`),
          )
        }
        if (!belongsToCurrentSession()) {
          sessionChanged = true
          break
        }
      }

      return { uploaded, errors, sessionChanged }
    },
    onSuccess: (result, variables) => {
      const outcome: DocumentUploadBatchOutcome | null =
        result.sessionChanged || result.errors.length === 0
          ? null
          : {
              uploadedCount: result.uploaded.length,
              errors: result.errors.map((error) => error.message),
              completedAt: new Date().toISOString(),
              projectId: variables.projectId,
              appId: variables.appId,
              q: variables.q,
            }
      queryClient.setQueryData<DocumentUploadBatchOutcome | null>(
        queryKeys.documents.uploadOutcome(),
        () => outcome,
      )
    },
    onError: (error, variables) => {
      queryClient.setQueryData<DocumentUploadBatchOutcome | null>(
        queryKeys.documents.uploadOutcome(),
        (): DocumentUploadBatchOutcome => ({
          uploadedCount: 0,
          errors: [error.message],
          completedAt: new Date().toISOString(),
          projectId: variables.projectId,
          appId: variables.appId,
          q: variables.q,
        }),
      )
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
