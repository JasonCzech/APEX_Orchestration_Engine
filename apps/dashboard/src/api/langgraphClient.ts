import type { Client } from '@langchain/langgraph-sdk'

import { getApiKey, subscribeApiKey } from '@/auth/keyStorage'
import { resolveLanggraphBaseUrl } from '@/config/runtimeConfig'
import { createDevLangGraphClient, subscribeDevDataMode } from '@/dev-data'

let clientPromise: Promise<Client> | null = null

/**
 * Lazy LangGraph SDK client factory: the SDK is dynamically imported on first
 * use (kept out of the entry chunk; rollup splits it into vendor-langgraph)
 * and rebuilt whenever the stored API key changes. The SDK sends the apiKey
 * as the `x-api-key` header — the same credential the /v1 surface uses.
 */
export function getLangGraphClient(): Promise<Client> {
  const devClient = createDevLangGraphClient()
  if (devClient) return Promise.resolve(devClient as Client)
  clientPromise ??= buildClient()
  return clientPromise
}

async function buildClient(): Promise<Client> {
  const { Client } = await import('@langchain/langgraph-sdk')
  return new Client({
    apiUrl: resolveLanggraphBaseUrl(),
    apiKey: getApiKey() ?? undefined,
  })
}

export function resetLangGraphClient(): void {
  clientPromise = null
}

subscribeApiKey(() => {
  resetLangGraphClient()
})

subscribeDevDataMode(() => {
  resetLangGraphClient()
})
