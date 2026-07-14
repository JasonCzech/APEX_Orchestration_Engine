import { useQuery, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { queryKeys } from '@/api/queryKeys'

export type DraftRead = components['schemas']['DraftRead']

export interface DraftWriteBody {
  title: string
  project_id?: string | null
  payload: Record<string, unknown>
}

/**
 * Imperative request helpers for the wizard's autosave loop (useDraft owns the
 * debounce/sequencing state machine, so these stay plain async functions; the
 * react-query surface is just the resume-draft list below).
 */
export async function createDraftRequest(
  body: DraftWriteBody,
): Promise<DraftRead> {
  const { data, error, response } = await getApexClient().POST('/v1/drafts', { body })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Draft create failed (${response.status})`),
      error,
    )
  }
  return data
}

export async function updateDraftRequest(draftId: string, body: DraftWriteBody): Promise<DraftRead> {
  const { data, error, response } = await getApexClient().PUT('/v1/drafts/{draft_id}', {
    params: { path: { draft_id: draftId } },
    body,
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Draft update failed (${response.status})`),
      error,
    )
  }
  return data
}

export async function getDraftRequest(draftId: string): Promise<DraftRead> {
  const { data, error, response } = await getApexClient().GET('/v1/drafts/{draft_id}', {
    params: { path: { draft_id: draftId } },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Draft load failed (${response.status})`),
      error,
    )
  }
  return data
}

export async function deleteDraftRequest(draftId: string): Promise<void> {
  const { error, response } = await getApexClient().DELETE('/v1/drafts/{draft_id}', {
    params: { path: { draft_id: draftId } },
  })
  if (!response.ok) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Draft delete failed (${response.status})`),
      error,
    )
  }
}

async function fetchDrafts(project?: string): Promise<DraftRead[]> {
  const { data, error, response } = await getApexClient().GET('/v1/drafts', {
    params: { query: project ? { project } : {} },
  })
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `Drafts request failed (${response.status})`),
      error,
    )
  }
  return data
}

/** Saved drafts for the wizard's "Resume draft" entry point. */
export function useDraftsList(
  project?: string,
  options: { enabled?: boolean } = {},
): UseQueryResult<DraftRead[], Error> {
  return useQuery({
    queryKey: queryKeys.drafts.list(project),
    queryFn: () => fetchDrafts(project),
    enabled: options.enabled ?? true,
    staleTime: 30_000,
  })
}
