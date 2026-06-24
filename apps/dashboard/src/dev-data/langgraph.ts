import type { Client } from '@langchain/langgraph-sdk'

import { getDevDataStore } from './controller'

type UsedLangGraphClient = Pick<Client, 'assistants' | 'threads' | 'runs'>

export function createDevLangGraphClient(): UsedLangGraphClient | null {
  const store = getDevDataStore()
  if (!store) return null

  return {
    assistants: {
      search: async () => store.searchAssistants(),
      get: async (assistantId: string) => store.getAssistant(assistantId),
      update: async (assistantId: string, payload: { config?: { configurable?: Record<string, unknown> } }) =>
        store.updateAssistant(assistantId, payload),
    } as UsedLangGraphClient['assistants'],
    threads: {
      create: async (payload?: { metadata?: Record<string, unknown> }) => store.createThread(payload),
    } as UsedLangGraphClient['threads'],
    runs: {
      create: async (
        threadId: string | null,
        assistantId: string,
        payload?: { input?: unknown; config?: { configurable?: Record<string, unknown> } },
      ) => store.createRun(threadId, assistantId, payload),
      list: async (threadId: string) => store.listRuns(threadId),
      joinStream: (threadId: string | undefined | null, runId: string, options?: { signal?: AbortSignal }) =>
        store.joinRunStream(threadId, runId, options),
    } as UsedLangGraphClient['runs'],
  }
}

