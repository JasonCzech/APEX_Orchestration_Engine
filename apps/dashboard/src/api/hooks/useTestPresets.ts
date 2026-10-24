/**
 * Test presets of both kinds behind one surface:
 * - team/personal presets: APEX `/v1/test-presets` (any operator, scoped);
 * - organization presets: LangGraph assistants (platform administrators).
 * `usePresets` merges them into the shared `Preset` shape.
 */
import { useMemo } from 'react'
import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { getApexClient } from '@/api/apexClient'
import { ApiError, errorMessageOf } from '@/api/errors'
import { useAssistants, type GoldenConfig } from '@/api/hooks/useAssistants'
import { getLangGraphClient } from '@/api/langgraphClient'
import { queryKeys } from '@/api/queryKeys'
import {
  organizationApplication,
  organizationSections,
  presetKey,
  type Preset,
  type PresetKind,
  type PresetSections,
} from '@/features/presets/presetModel'

export type TestPresetRead = components['schemas']['TestPresetRead']
export type TestPresetVersionRead = components['schemas']['TestPresetVersionRead']
export type PipelinePresetLink = components['schemas']['PipelinePresetLink']

export const presetKeys = {
  all: ['test-presets'] as const,
  team: (includeArchived: boolean) => [...presetKeys.all, 'team', { includeArchived }] as const,
  teamDetail: (id: string) => [...presetKeys.all, 'team-detail', id] as const,
  versions: (id: string) => [...presetKeys.all, 'versions', id] as const,
  usage: (kind: PresetKind, ids: readonly string[]) => [...presetKeys.all, 'usage', kind, [...ids].sort()] as const,
  run: (threadId: string) => [...presetKeys.all, 'run', threadId] as const,
}

function fail(status: number, error: unknown, fallback: string): never {
  throw new ApiError(status, errorMessageOf(error, fallback), error)
}

export function teamPreset(row: TestPresetRead): Preset {
  const payload = row.payload as { sections?: PresetSections }
  return {
    key: presetKey('team', row.id),
    kind: 'team',
    id: row.id,
    name: row.name,
    description: row.description ?? null,
    applicationId: row.application_id ?? null,
    projectId: row.project_id,
    visibility: row.visibility,
    version: row.version,
    updatedAt: row.updated_at ?? null,
    updatedBy: row.updated_by ?? null,
    owner: row.created_by ?? null,
    mine: row.mine,
    editable: row.editable,
    archived: Boolean(row.archived_at),
    sections: payload.sections ?? {},
  }
}

const TEAM_PAGE = 200

/** Every page: the server pages over presets the caller can see, so a short page is the last. */
async function fetchTeamPresets(includeArchived: boolean, signal?: AbortSignal): Promise<TestPresetRead[]> {
  const rows: TestPresetRead[] = []
  for (;;) {
    const { data, error, response } = await getApexClient().GET('/v1/test-presets', {
      params: { query: { include_archived: includeArchived, limit: TEAM_PAGE, offset: rows.length } },
      signal,
    })
    if (!response.ok || !data) fail(response.status, error, `Test presets failed to load (${response.status})`)
    rows.push(...data)
    if (data.length < TEAM_PAGE) return rows
  }
}

export function useTeamPresets(options: { includeArchived?: boolean } = {}): UseQueryResult<Preset[], Error> {
  const includeArchived = options.includeArchived ?? false
  return useQuery({
    queryKey: presetKeys.team(includeArchived),
    queryFn: async ({ signal }) => (await fetchTeamPresets(includeArchived, signal)).map(teamPreset),
    staleTime: 30_000,
  })
}

export function useTeamPreset(id: string | undefined): UseQueryResult<Preset, Error> {
  return useQuery({
    queryKey: presetKeys.teamDetail(id ?? ''),
    enabled: Boolean(id),
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/test-presets/{preset_id}', {
        params: { path: { preset_id: id! } },
        signal,
      })
      if (!response.ok || !data) fail(response.status, error, `Test preset failed to load (${response.status})`)
      return teamPreset(data)
    },
  })
}

export function usePresetVersions(id: string | undefined): UseQueryResult<TestPresetVersionRead[], Error> {
  return useQuery({
    queryKey: presetKeys.versions(id ?? ''),
    enabled: Boolean(id),
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/test-presets/{preset_id}/versions', {
        params: { path: { preset_id: id! } },
        signal,
      })
      if (!response.ok || !data) fail(response.status, error, `Preset history failed to load (${response.status})`)
      return data
    },
  })
}

export interface PresetUsageStats {
  tests: number
  lastUsedAt: string | null
}

/** Tests started from these presets (counted over tests you can see), keyed by preset key. */
export function usePresetUsage(presets: readonly Pick<Preset, 'kind' | 'id'>[]): UseQueryResult<Map<string, PresetUsageStats>, Error> {
  const team = presets.filter(p => p.kind === 'team').map(p => p.id)
  const organization = presets.filter(p => p.kind === 'organization').map(p => p.id)
  return useQuery({
    queryKey: [...presetKeys.usage('team', team), ...presetKeys.usage('organization', organization)],
    enabled: presets.length > 0,
    staleTime: 60_000,
    queryFn: async ({ signal }) => {
      const result = new Map<string, PresetUsageStats>()
      for (const [kind, ids] of [['team', team], ['organization', organization]] as const) {
        for (let start = 0; start < ids.length; start += 200) {
          const { data, error, response } = await getApexClient().GET('/v1/test-presets/usage', {
            params: { query: { kind, ids: ids.slice(start, start + 200) } },
            signal,
          })
          if (!response.ok || !data) fail(response.status, error, `Preset usage failed to load (${response.status})`)
          for (const row of data) result.set(presetKey(kind, row.preset_id), { tests: row.tests, lastUsedAt: row.last_used_at ?? null })
        }
      }
      return result
    },
  })
}

/** The preset a test started from (null when it didn't start from one). */
export function useRunPreset(threadId: string | undefined): UseQueryResult<PipelinePresetLink | null, Error> {
  return useQuery({
    queryKey: presetKeys.run(threadId ?? ''),
    enabled: Boolean(threadId),
    staleTime: 5 * 60_000,
    queryFn: async ({ signal }) => {
      const { data, error, response } = await getApexClient().GET('/v1/pipelines/{thread_id}/preset', {
        params: { path: { thread_id: threadId! } },
        signal,
      })
      if (!response.ok) fail(response.status, error, `Preset link failed to load (${response.status})`)
      return data ?? null
    },
  })
}

/** An organization preset (a `pipeline` assistant) in the shared shape. */
export function organizationPreset(golden: GoldenConfig): Preset {
  return {
    key: presetKey('organization', golden.assistantId),
    kind: 'organization',
    id: golden.assistantId,
    name: golden.name,
    description: golden.description,
    applicationId: organizationApplication(golden.configurable, golden.metadata),
    projectId: typeof golden.configurable['project_id'] === 'string' && golden.configurable['project_id'] ? golden.configurable['project_id'] : null,
    visibility: 'organization',
    version: golden.version,
    updatedAt: golden.updatedAt,
    updatedBy: typeof golden.metadata['updated_by'] === 'string' ? golden.metadata['updated_by'] : null,
    owner: typeof golden.metadata['created_by'] === 'string' ? golden.metadata['created_by'] : null,
    mine: false,
    editable: false,
    archived: false,
    sections: organizationSections(golden.assistantId, golden.configurable),
  }
}

/** Organization presets (assistants) and team presets in one list. */
export function usePresets(options: { includeArchived?: boolean } = {}) {
  const organization = useAssistants()
  const team = useTeamPresets(options)
  // Stable identity between renders, so effects keyed on the list don't loop.
  const data = useMemo<Preset[] | undefined>(
    () => organization.data || team.data
      ? [...(team.data ?? []), ...(organization.data ?? []).map(organizationPreset)]
      : undefined,
    [organization.data, team.data],
  )
  return {
    data,
    isPending: organization.isPending || team.isPending,
    isError: organization.isError && team.isError,
    /** One source failed; the other's presets are still listed. */
    partialError: (organization.isError || team.isError) && !(organization.isError && team.isError)
      ? (organization.error ?? team.error)
      : null,
    error: organization.error ?? team.error,
    refetch: () => { void organization.refetch(); void team.refetch() },
  }
}

// ── Writes ───────────────────────────────────────────────────────────────────

export interface TeamPresetWrite {
  name: string
  description: string | null
  application_id: string | null
  visibility: 'personal' | 'project'
  payload: { schema_version: 1; sections: PresetSections }
}

function useInvalidatePresets() {
  const queryClient = useQueryClient()
  return () => {
    void queryClient.invalidateQueries({ queryKey: presetKeys.all })
    void queryClient.invalidateQueries({ queryKey: queryKeys.goldenConfigs.all })
  }
}

export function useCreateTeamPreset() {
  const invalidate = useInvalidatePresets()
  return useMutation<Preset, Error, TeamPresetWrite & { project_id: string }>({
    mutationFn: async (body) => {
      const { data, error, response } = await getApexClient().POST('/v1/test-presets', { body })
      if (!response.ok || !data) fail(response.status, error, `Preset save failed (${response.status})`)
      return teamPreset(data)
    },
    onSuccess: invalidate,
  })
}

export function useUpdateTeamPreset() {
  const invalidate = useInvalidatePresets()
  return useMutation<Preset, Error, { id: string; body: TeamPresetWrite & { expected_version?: number | null; note?: string | null } }>({
    mutationFn: async ({ id, body }) => {
      const { data, error, response } = await getApexClient().PUT('/v1/test-presets/{preset_id}', {
        params: { path: { preset_id: id } },
        body,
      })
      if (!response.ok || !data) fail(response.status, error, `Preset update failed (${response.status})`)
      return teamPreset(data)
    },
    onSuccess: invalidate,
    // A conflict means our copy is stale; reload it so the next attempt can succeed.
    onError: (error) => { if (error instanceof ApiError && error.status === 409) invalidate() },
  })
}

export function useSetTeamPresetArchived() {
  const invalidate = useInvalidatePresets()
  return useMutation<Preset, Error, { id: string; archived: boolean }>({
    mutationFn: async ({ id, archived }) => {
      const path = archived ? '/v1/test-presets/{preset_id}/archive' : '/v1/test-presets/{preset_id}/unarchive'
      const { data, error, response } = await getApexClient().POST(path, { params: { path: { preset_id: id } } })
      if (!response.ok || !data) fail(response.status, error, `Preset ${archived ? 'archive' : 'unarchive'} failed (${response.status})`)
      return teamPreset(data)
    },
    onSuccess: invalidate,
  })
}

export function useDeleteTeamPreset() {
  const invalidate = useInvalidatePresets()
  return useMutation<void, Error, string>({
    mutationFn: async (id) => {
      const { error, response } = await getApexClient().DELETE('/v1/test-presets/{preset_id}', {
        params: { path: { preset_id: id } },
      })
      if (!response.ok) fail(response.status, error, `Preset delete failed (${response.status})`)
    },
    onSuccess: invalidate,
  })
}

/** Publish (or update) an organization preset: a `pipeline` assistant. Platform admins only. */
export function useSaveOrganizationPreset() {
  const invalidate = useInvalidatePresets()
  return useMutation<string, Error, { assistantId?: string | null; name: string; description: string | null; applicationId: string | null; configurable: Record<string, unknown> }>({
    mutationFn: async ({ assistantId, name, description, applicationId, configurable }) => {
      const client = await getLangGraphClient()
      const metadata = applicationId ? { application_id: applicationId } : {}
      const saved = assistantId
        ? await client.assistants.update(assistantId, { name, description: description ?? undefined, metadata, config: { configurable } })
        : await client.assistants.create({ graphId: 'pipeline', name, description: description ?? undefined, metadata, config: { configurable } })
      return saved.assistant_id
    },
    onSuccess: invalidate,
  })
}
