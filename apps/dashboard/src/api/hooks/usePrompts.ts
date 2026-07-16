/**
 * Prompt catalog hooks (D5, plan UX 2.e) — typed via @apex/api-client over
 * /v1/prompts. The REST surface addresses prompts by catalog id while the
 * dashboard routes address them by (namespace, key); usePrompt bridges the
 * two with a list-then-detail fetch keyed on queryKeys.prompts.detail(ns, key)
 * so every screen under /prompts/:ns/:name shares one cache entry.
 */
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { fetchAllOffsetPages, findInOffsetPages } from '@/api/fetchAllPages'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type PromptSummary = components['schemas']['PromptSummary']
export type PromptDetail = components['schemas']['PromptDetail']
export type PromptVersionInfo = components['schemas']['PromptVersionInfo']
export type PromptVersionDetail = components['schemas']['PromptVersionDetail']
export type CreatePromptRequest = components['schemas']['CreatePromptRequest']
export type TestPromptRequest = components['schemas']['TestPromptRequest']
export type TestPromptResponse = components['schemas']['TestPromptResponse']

export interface PromptPlaygroundRun {
  runId: string
  threadId: string | null
  at: string
  label: string
}

export interface TestPromptSubmission {
  request: TestPromptRequest
  history: {
    promptId: string
    projectId: string
    appId: string
    label: string
  }
}

export interface PromptPlaygroundSelection {
  projectId: string
  appId: string
}

export interface PromptPlaygroundSelectionState {
  selection: PromptPlaygroundSelection
  setSelection: (selection: PromptPlaygroundSelection) => void
}

export function promptWriteMutationKey(promptId: string | undefined) {
  return ['prompts', 'write', promptId ?? ''] as const
}

export function promptWriteMutationScopeId(promptId: string | undefined): string {
  return `prompts:write:${promptId ?? ''}`
}

export function promptTestMutationKey(promptId: string | undefined) {
  return ['prompts', 'test', promptId ?? ''] as const
}

export function promptTestMutationScopeId(promptId: string | undefined): string {
  return `prompts:test:${promptId ?? ''}`
}

/** Server-side filters for GET /v1/prompts (namespace stays client-side: the tree needs all). */
export interface PromptListFilters {
  includeArchived?: boolean
  q?: string
}

// ── Fetchers ─────────────────────────────────────────────────────────────────

const PROMPT_PAGE_SIZE = 200

async function fetchPromptList(
  filters: PromptListFilters,
  signal?: AbortSignal,
): Promise<PromptSummary[]> {
  return fetchAllOffsetPages({
    label: 'Prompt list',
    pageSize: PROMPT_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/prompts', {
        params: {
          query: {
            ...(filters.includeArchived ? { include_archived: true } : {}),
            ...(filters.q ? { q: filters.q } : {}),
            limit,
            offset,
          },
        },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Prompt list failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

async function fetchPromptDetail(promptId: string, signal?: AbortSignal): Promise<PromptDetail> {
  const { data, error, response } = await getApexClient().GET('/v1/prompts/{prompt_id}', {
    params: { path: { prompt_id: promptId } },
    signal,
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Prompt load failed (${response.status})`),
      error,
    )
  }
  return data
}

/** Resolves (namespace, key) -> id via the namespace-scoped list, then loads the detail. */
async function fetchPromptByKey(
  namespace: string,
  key: string,
  signal?: AbortSignal,
): Promise<PromptDetail> {
  const row = await findInOffsetPages({
    label: 'Prompt lookup',
    pageSize: PROMPT_PAGE_SIZE,
    predicate: (entry: PromptSummary) => entry.namespace === namespace && entry.key === key,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET('/v1/prompts', {
        params: {
          query: {
            namespace,
            include_archived: true,
            limit,
            offset,
          },
        },
        signal,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Prompt lookup failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
  if (row) return fetchPromptDetail(row.id, signal)
  throw new ApiError(404, `Prompt ${namespace}/${key} was not found in the catalog.`)
}

async function fetchPromptVersions(
  promptId: string,
  signal?: AbortSignal,
): Promise<PromptVersionInfo[]> {
  return fetchAllOffsetPages({
    label: 'Prompt versions',
    pageSize: PROMPT_PAGE_SIZE,
    fetchPage: async (limit, offset) => {
      const { data, error, response } = await getApexClient().GET(
        '/v1/prompts/{prompt_id}/versions',
        {
          params: {
            path: { prompt_id: promptId },
            query: { limit, offset },
          },
          signal,
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Version history failed (${response.status})`),
          error,
        )
      }
      return data
    },
  })
}

async function fetchPromptVersion(
  promptId: string,
  versionId: string,
  signal?: AbortSignal,
): Promise<PromptVersionDetail> {
  const { data, error, response } = await getApexClient().GET(
    '/v1/prompts/{prompt_id}/versions/{version_id}',
    { params: { path: { prompt_id: promptId, version_id: versionId } }, signal },
  )
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Version load failed (${response.status})`),
      error,
    )
  }
  return data
}

// ── Queries ──────────────────────────────────────────────────────────────────

/** Browser list (all namespaces — the namespace tree derives from the rows). */
export function usePromptList(
  filters: PromptListFilters = {},
): UseQueryResult<PromptSummary[], Error> {
  const normalized: PromptListFilters = {
    ...(filters.includeArchived ? { includeArchived: true } : {}),
    ...(filters.q ? { q: filters.q } : {}),
  }
  return useQuery({
    queryKey: queryKeys.prompts.listWith(normalized as Record<string, unknown>),
    queryFn: ({ signal }) => fetchPromptList(normalized, signal),
    placeholderData: keepPreviousData,
    staleTime: STALE_TIMES.prompts,
  })
}

/** Prompt detail addressed the way the routes are: by (namespace, key). */
export function usePrompt(ns: string, key: string): UseQueryResult<PromptDetail, Error> {
  return useQuery({
    queryKey: queryKeys.prompts.detail(ns, key),
    queryFn: ({ signal }) => fetchPromptByKey(ns, key, signal),
    staleTime: STALE_TIMES.prompts,
    enabled: ns.length > 0 && key.length > 0,
  })
}

/** Version history (no content). Waits for the detail fetch to supply the id. */
export function usePromptVersions(
  ns: string,
  key: string,
  promptId: string | undefined,
): UseQueryResult<PromptVersionInfo[], Error> {
  return useQuery({
    queryKey: queryKeys.prompts.versions(ns, key),
    queryFn: ({ signal }) => fetchPromptVersions(promptId ?? '', signal),
    staleTime: STALE_TIMES.prompts,
    enabled: Boolean(promptId),
  })
}

/** One version with content (version page + diff comparisons). */
export function usePromptVersion(
  ns: string,
  key: string,
  versionId: string | undefined,
  promptId: string | undefined,
): UseQueryResult<PromptVersionDetail, Error> {
  return useQuery({
    queryKey: queryKeys.prompts.version(ns, key, versionId ?? ''),
    queryFn: ({ signal }) => fetchPromptVersion(promptId ?? '', versionId ?? '', signal),
    staleTime: STALE_TIMES.prompts,
    enabled: Boolean(promptId) && Boolean(versionId),
  })
}

// ── Mutations ────────────────────────────────────────────────────────────────

/** POST /v1/prompts — seeds the detail cache so create -> navigate renders instantly. */
export function useCreatePrompt(): UseMutationResult<PromptDetail, Error, CreatePromptRequest> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: async (body: CreatePromptRequest) => {
      const { data, error, response } = await getApexClient().POST('/v1/prompts', { body })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Create prompt failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (created) => {
      queryClient.setQueryData(queryKeys.prompts.detail(created.namespace, created.key), created)
      void queryClient.invalidateQueries({ queryKey: queryKeys.prompts.all })
    },
  })
}

export interface SaveVersionInput {
  content: string
  note?: string
}

/** POST /{id}/versions — the server moves the active pointer to the new version. */
export function useSaveVersion(
  promptId: string | undefined,
): UseMutationResult<PromptVersionDetail, Error, SaveVersionInput> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: promptWriteMutationKey(promptId),
    scope: { id: promptWriteMutationScopeId(promptId) },
    mutationFn: async ({ content, note }: SaveVersionInput) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/prompts/{prompt_id}/versions',
        {
          params: { path: { prompt_id: promptId ?? '' } },
          body: { content, ...(note ? { note } : {}) },
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Save version failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.prompts.all })
    },
  })
}

/** POST /{id}/rollback {version_id} — 409 means the version belongs to another prompt. */
export function useRollbackPrompt(
  ns: string,
  key: string,
  promptId: string | undefined,
): UseMutationResult<PromptDetail, Error, string> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: promptWriteMutationKey(promptId),
    scope: { id: promptWriteMutationScopeId(promptId) },
    mutationFn: async (versionId: string) => {
      const { data, error, response } = await getApexClient().POST(
        '/v1/prompts/{prompt_id}/rollback',
        {
          params: { path: { prompt_id: promptId ?? '' } },
          body: { version_id: versionId },
        },
      )
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Rollback failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (detail) => {
      queryClient.setQueryData(queryKeys.prompts.detail(ns, key), detail)
      void queryClient.invalidateQueries({ queryKey: queryKeys.prompts.all })
    },
  })
}

/**
 * POST /{id}/archive | /unarchive — optimistic on the detail cache with
 * revert-on-error (plan UX 2.e: the header chip flips instantly).
 */
export function useSetArchived(
  ns: string,
  key: string,
  promptId: string | undefined,
): UseMutationResult<
  PromptSummary,
  Error,
  boolean,
  { previous: PromptDetail | undefined } | undefined
> {
  const queryClient = useQueryClient()
  const detailKey = queryKeys.prompts.detail(ns, key)
  return useMutation({
    mutationKey: promptWriteMutationKey(promptId),
    scope: { id: promptWriteMutationScopeId(promptId) },
    mutationFn: async (archived: boolean) => {
      const client = getApexClient()
      const request = archived
        ? client.POST('/v1/prompts/{prompt_id}/archive', {
            params: { path: { prompt_id: promptId ?? '' } },
          })
        : client.POST('/v1/prompts/{prompt_id}/unarchive', {
            params: { path: { prompt_id: promptId ?? '' } },
          })
      const { data, error, response } = await request
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(
            error,
            `${archived ? 'Archive' : 'Unarchive'} failed (${response.status})`,
          ),
          error,
        )
      }
      return data
    },
    onMutate: async (archived) => {
      await queryClient.cancelQueries({ queryKey: detailKey })
      const previous = queryClient.getQueryData<PromptDetail>(detailKey)
      if (previous) {
        queryClient.setQueryData<PromptDetail>(detailKey, {
          ...previous,
          archived_at: archived ? new Date().toISOString() : null,
        })
      }
      return { previous }
    },
    onError: (_error, _archived, context) => {
      if (context?.previous) queryClient.setQueryData(detailKey, context.previous)
    },
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.prompts.all })
    },
  })
}

/**
 * Session-local accepted handles live in the query cache so a route remount
 * can observe a POST that completed after its original mutation observer left.
 * Authentication changes clear this cache and the REST middleware rejects
 * responses from superseded credential or semantic-session revisions.
 */
export function usePromptTestHistory(
  promptId: string | undefined,
  projectId: string,
  appId: string,
): UseQueryResult<PromptPlaygroundRun[], Error> {
  return useQuery({
    queryKey: queryKeys.prompts.playgroundHistory(promptId ?? '', projectId, appId),
    queryFn: async () => [],
    initialData: [],
    // This is client-owned session state. Keeping it disabled prevents broad
    // prompt-catalog invalidations from replacing accepted handles with [].
    enabled: false,
    staleTime: Infinity,
    gcTime: Infinity,
  })
}

/**
 * The selected audience is client-owned session state. Persisting it beside
 * the accepted-run history keeps a manually scoped submission observable when
 * the route remounts before or after the request completes.
 */
export function usePromptPlaygroundSelection(
  promptId: string | undefined,
  fallback: PromptPlaygroundSelection,
): PromptPlaygroundSelectionState {
  const queryClient = useQueryClient()
  const queryKey = queryKeys.prompts.playgroundSelection(promptId ?? '')
  const selection = useQuery({
    queryKey,
    queryFn: async (): Promise<PromptPlaygroundSelection> => fallback,
    initialData: fallback,
    enabled: false,
    staleTime: Infinity,
    gcTime: Infinity,
  })

  return {
    selection: selection.data,
    setSelection: (next) => {
      queryClient.setQueryData<PromptPlaygroundSelection>(queryKey, next)
    },
  }
}

/** POST /{id}/test — 202 stateless playground run with session-local handle publication. */
export function useTestPrompt(
  promptId: string | undefined,
): UseMutationResult<TestPromptResponse, Error, TestPromptSubmission> {
  const queryClient = useQueryClient()
  return useMutation({
    mutationKey: promptTestMutationKey(promptId),
    scope: { id: promptTestMutationScopeId(promptId) },
    mutationFn: async ({ request }: TestPromptSubmission) => {
      const { data, error, response } = await getApexClient().POST('/v1/prompts/{prompt_id}/test', {
        params: { path: { prompt_id: promptId ?? '' } },
        body: request,
      })
      if (!response.ok || !data) {
        throw new ApiError(
          response.status,
          errorMessageOf(error, `Test run failed (${response.status})`),
          error,
        )
      }
      return data
    },
    onSuccess: (accepted, submission) => {
      const { promptId: submittedPromptId, projectId, appId, label } = submission.history
      queryClient.setQueryData<PromptPlaygroundRun[]>(
        queryKeys.prompts.playgroundHistory(submittedPromptId, projectId, appId),
        (previous = []) => [
          {
            runId: accepted.run_id,
            threadId: accepted.thread_id ?? null,
            at: new Date().toISOString(),
            label,
          },
          ...previous,
        ],
      )
    },
  })
}
