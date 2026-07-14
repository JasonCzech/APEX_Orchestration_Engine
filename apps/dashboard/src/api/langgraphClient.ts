import type { Client } from '@langchain/langgraph-sdk'

import { getApiKey, getApiKeyRevision, subscribeApiKey } from '@/auth/keyStorage'
import { notifyUnauthorized } from '@/api/apexClient'
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
  if (!clientPromise) {
    const pending = buildClient()
    clientPromise = pending
    // A failed dynamic import or constructor must not poison the singleton for
    // the rest of the page lifetime. Only clear the promise we started, so a
    // concurrent reset/new build cannot be overwritten by this rejection.
    void pending.catch(() => {
      if (clientPromise === pending) clientPromise = null
    })
  }
  return clientPromise
}

async function buildClient(): Promise<Client> {
  const requestKey = getApiKey()
  const requestRevision = getApiKeyRevision()
  const { Client } = await import('@langchain/langgraph-sdk')
  const authFetch: typeof fetch = async (input, init) => {
    const response = await fetch(input, init)
    const requestKey = new Request(input, init).headers.get('x-api-key')
    if (
      response.status === 401 &&
      requestKey &&
      requestKey === getApiKey() &&
      requestRevision === getApiKeyRevision()
    ) {
      notifyUnauthorized()
    }
    return response
  }
  return new Client({
    apiUrl: resolveLanggraphBaseUrl(),
    apiKey: requestKey ?? undefined,
    callerOptions: { fetch: authFetch, maxRetries: 0 },
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
