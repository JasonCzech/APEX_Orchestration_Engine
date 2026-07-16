import createClient, { type Middleware } from 'openapi-fetch'

import type { components, paths } from '@apex/api-client'

import { getApiKey, getApiKeyRevision } from '@/auth/keyStorage'
import { resolveApexBaseUrl } from '@/config/runtimeConfig'
import { getDevApexFetch, subscribeDevDataMode } from '@/dev-data'

import { ApiError, errorMessageOf } from './errors'
import { fetchWithoutRedirects } from './fetchPolicy'

export type ApexClient = ReturnType<typeof createClient<paths>>
export type SystemInfo = components['schemas']['SystemInfo']
export type ConsumerInfo = components['schemas']['ConsumerInfo']
export type Role = components['schemas']['Role']

type UnauthorizedHandler = () => void

const unauthorizedHandlers = new Set<UnauthorizedHandler>()
const requestAuthRevisions = new WeakMap<Request, number>()

/** Registered by AuthProvider so any 401 anywhere drops the session. */
export function onUnauthorized(handler: UnauthorizedHandler): () => void {
  unauthorizedHandlers.add(handler)
  return () => {
    unauthorizedHandlers.delete(handler)
  }
}

/** Notify the auth provider about unauthorized requests made outside openapi-fetch. */
export function notifyUnauthorized(): void {
  for (const handler of unauthorizedHandlers) handler()
}

const authMiddleware: Middleware = {
  onRequest({ request }) {
    const key = getApiKey()
    if (key) request.headers.set('x-api-key', key)
    requestAuthRevisions.set(request, getApiKeyRevision())
    return request
  },
  onResponse({ request, response }) {
    const requestKey = request.headers.get('x-api-key')
    const belongsToCurrentSession =
      requestKey !== null &&
      requestKey === getApiKey() &&
      requestAuthRevisions.get(request) === getApiKeyRevision()
    if (response.status === 401 && belongsToCurrentSession) {
      for (const handler of unauthorizedHandlers) handler()
    }
    return response
  },
}

let client: ApexClient | null = null

/**
 * Lazy singleton typed from the generated @apex/api-client schema. The schema
 * paths already include the /v1 prefix, so baseUrl is the APEX origin only
 * (same origin when runtime config leaves apexOrigin empty).
 */
export function getApexClient(): ApexClient {
  if (!client) {
    const transport = getDevApexFetch() ?? globalThis.fetch
    client = createClient<paths>({
      baseUrl: resolveApexBaseUrl(),
      fetch: (input: Request) => fetchWithoutRedirects(input, undefined, transport),
    })
    client.use(authMiddleware)
  }
  return client
}

/** Rebuild the client after runtime-config changes (also used by tests). */
export function resetApexClient(): void {
  client = null
}

export async function fetchSystemInfo(): Promise<SystemInfo> {
  // The spec declares no error responses for this op, so openapi-fetch's
  // typed `error` branch collapses to never — gate on response.ok instead
  // (401/5xx still happen at runtime).
  const { data, error, response } = await getApexClient().GET('/v1/system/info')
  if (!response.ok || !data) {
    throw new ApiError(
      response.status,
      errorMessageOf(error, `System info request failed (${response.status})`),
      error,
    )
  }
  return data
}

subscribeDevDataMode(() => {
  resetApexClient()
})
