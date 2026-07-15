import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import type { Assistant } from '@langchain/langgraph-sdk'

import { getLangGraphClient } from '@/api/langgraphClient'
import { queryKeys } from '@/api/queryKeys'

/**
 * A golden configuration = a LangGraph assistant on the `pipeline` graph
 * pinning a `config.configurable` bundle (plan "Golden configurations").
 * Mapped down to the fields the wizard's picker needs.
 */
export interface GoldenConfig {
  assistantId: string
  name: string
  description: string | null
  /** The pinned configurable bundle ({} when the assistant pins nothing). */
  configurable: Record<string, unknown>
}

async function fetchGoldenConfigs(): Promise<GoldenConfig[]> {
  const assistants = await fetchBoundedPipelineAssistants()
  return assistants
    .filter((assistant) => assistant.metadata?.['created_by'] !== 'system')
    .map((assistant) => ({
      assistantId: assistant.assistant_id,
      name: assistant.name,
      description: assistant.description ?? null,
      configurable: (assistant.config?.configurable ?? {}) as Record<string, unknown>,
    }))
}

const ASSISTANT_PAGE_SIZE = 5
const MAX_GOLDEN_CONFIGS = 20

async function fetchBoundedPipelineAssistants(): Promise<Assistant[]> {
  const client = await getLangGraphClient()
  const summaries: Assistant[] = []
  for (let offset = 0; offset < MAX_GOLDEN_CONFIGS; offset += ASSISTANT_PAGE_SIZE) {
    const page = await client.assistants.search({
      graphId: 'pipeline',
      limit: ASSISTANT_PAGE_SIZE,
      offset,
      select: [
        'assistant_id',
        'graph_id',
        'name',
        'description',
        'created_at',
        'updated_at',
        'version',
      ],
    })
    summaries.push(...page)
    if (page.length < ASSISTANT_PAGE_SIZE) break
  }
  const assistants: Assistant[] = []
  for (const summary of summaries) {
    assistants.push(await client.assistants.get(summary.assistant_id))
  }
  return assistants
}

/**
 * Golden-config picker source (wizard Config step). The langgraph dev server's
 * auto-created default assistant carries metadata.created_by === "system" and
 * is filtered out — it pins nothing and is not a golden config.
 */
export function useAssistants(): UseQueryResult<GoldenConfig[], Error> {
  return useQuery({
    queryKey: queryKeys.goldenConfigs.list(),
    queryFn: fetchGoldenConfigs,
    staleTime: 60_000,
  })
}

// ── D7 appends below — /golden-configs screens (list, detail, edit) ──────────

/**
 * D7: a /golden-configs entry. Unlike GoldenConfig (the wizard picker source),
 * this KEEPS the dev server's system-created default assistant — the list
 * screen shows it with a "system default" chip instead of hiding it.
 */
export interface GoldenConfigEntry extends GoldenConfig {
  /** metadata.created_by === "system" (the auto-created `pipeline` assistant). */
  isSystemDefault: boolean
  /** Assistant version — bumps on every assistants.update. */
  version: number
  updatedAt: string
}

function toGoldenConfigEntry(assistant: Assistant): GoldenConfigEntry {
  return {
    assistantId: assistant.assistant_id,
    name: assistant.name,
    description: assistant.description ?? null,
    configurable: (assistant.config?.configurable ?? {}) as Record<string, unknown>,
    isSystemDefault: assistant.metadata?.['created_by'] === 'system',
    version: assistant.version,
    updatedAt: assistant.updated_at,
  }
}

async function fetchGoldenConfigsIndex(): Promise<GoldenConfigEntry[]> {
  const assistants = await fetchBoundedPipelineAssistants()
  return assistants.map(toGoldenConfigEntry)
}

/** D7: full assistants index for /golden-configs (system default included). */
export function useGoldenConfigsIndex(): UseQueryResult<GoldenConfigEntry[], Error> {
  return useQuery({
    queryKey: queryKeys.goldenConfigs.index(),
    queryFn: fetchGoldenConfigsIndex,
    staleTime: 60_000,
  })
}

async function fetchGoldenConfig(assistantId: string): Promise<GoldenConfigEntry> {
  const client = await getLangGraphClient()
  return toGoldenConfigEntry(await client.assistants.get(assistantId))
}

/** D7: one assistant for /golden-configs/:assistantId (SDK assistants.get). */
export function useGoldenConfig(assistantId: string): UseQueryResult<GoldenConfigEntry, Error> {
  return useQuery({
    queryKey: queryKeys.goldenConfigs.detail(assistantId),
    queryFn: () => fetchGoldenConfig(assistantId),
    staleTime: 60_000,
  })
}

export interface UpdateGoldenConfigInput {
  assistantId: string
  /** The full replacement config.configurable bundle. */
  configurable: Record<string, unknown>
}

/**
 * D7: replace an assistant's pinned configurable via SDK assistants.update
 * (verified browser-exposed: PATCH /assistants/{id}; the server bumps the
 * assistant version). Detail cache is patched from the response; both list
 * caches (wizard picker + index) are invalidated.
 */
export function useUpdateGoldenConfig() {
  const queryClient = useQueryClient()
  return useMutation<GoldenConfigEntry, Error, UpdateGoldenConfigInput>({
    mutationFn: async ({ assistantId, configurable }) => {
      const client = await getLangGraphClient()
      return toGoldenConfigEntry(
        await client.assistants.update(assistantId, { config: { configurable } }),
      )
    },
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.goldenConfigs.detail(updated.assistantId), updated)
      void queryClient.invalidateQueries({ queryKey: queryKeys.goldenConfigs.list() })
      void queryClient.invalidateQueries({ queryKey: queryKeys.goldenConfigs.index() })
    },
  })
}
