import { useQuery, type UseQueryResult } from '@tanstack/react-query'

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
  const client = await getLangGraphClient()
  const assistants = await client.assistants.search({ graphId: 'pipeline', limit: 100 })
  return assistants
    .filter((assistant) => assistant.metadata?.['created_by'] !== 'system')
    .map((assistant) => ({
      assistantId: assistant.assistant_id,
      name: assistant.name,
      description: assistant.description ?? null,
      configurable: (assistant.config?.configurable ?? {}) as Record<string, unknown>,
    }))
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
