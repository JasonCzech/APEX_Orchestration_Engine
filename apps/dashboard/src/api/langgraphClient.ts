import type { Client } from '@langchain/langgraph-sdk'

import {
  getApiKey,
  getApiKeyRevision,
  getSessionRevision,
  subscribeApiKey,
  subscribeSession,
} from '@/auth/keyStorage'
import { notifyUnauthorized } from '@/api/apexClient'
import { resolveLanggraphBaseUrl } from '@/config/runtimeConfig'
import { createDevLangGraphClient, subscribeDevDataMode } from '@/dev-data'

import { fetchWithoutRedirects } from './fetchPolicy'

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
  const requestSessionRevision = getSessionRevision()
  const { Client } = await import('@langchain/langgraph-sdk')
  if (
    requestRevision !== getApiKeyRevision() ||
    requestSessionRevision !== getSessionRevision() ||
    requestKey !== getApiKey()
  ) {
    throw new Error('Authentication changed while the LangGraph client was loading')
  }
  const authFetch: typeof fetch = async (input, init) => {
    if (
      requestRevision !== getApiKeyRevision() ||
      requestSessionRevision !== getSessionRevision() ||
      requestKey !== getApiKey()
    ) {
      throw new Error('Authentication changed while using the LangGraph client')
    }
    const responseKey = new Headers(
      init?.headers ??
        (input instanceof Request ? input.headers : undefined),
    ).get('x-api-key')
    const response = await fetchWithoutRedirects(input, init)
    const belongsToCurrentSession =
      requestRevision === getApiKeyRevision() &&
      requestSessionRevision === getSessionRevision() &&
      requestKey === getApiKey()
    if (!belongsToCurrentSession) {
      void response.body?.cancel().catch(() => undefined)
      throw new Error('Authentication changed while the request was in flight.')
    }
    if (
      response.status === 401 &&
      responseKey &&
      responseKey === getApiKey() &&
      requestRevision === getApiKeyRevision() &&
      requestSessionRevision === getSessionRevision()
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

subscribeSession(() => {
  resetLangGraphClient()
})

subscribeDevDataMode(() => {
  resetLangGraphClient()
})
