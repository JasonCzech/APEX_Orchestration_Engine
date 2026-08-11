import { isDemoDataMode } from '@/config/runtimeConfig'

import { getDevDataStore } from './controller'

export async function handleDevApexRequest(request: Request): Promise<Response | null> {
  return getDevDataStore()?.handleApexRequest(request) ?? null
}

export function getDevApexFetch(): typeof fetch | undefined {
  if (!getDevDataStore()) return undefined
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init)
    const response = await handleDevApexRequest(request)
    if (response) return response
    if (isDemoDataMode()) {
      return new Response(JSON.stringify({ type: 'about:blank', title: 'demo_handler_missing', status: 501, detail: `Offline demo blocked an unhandled request: ${request.method} ${new URL(request.url).pathname}` }), { status: 501, headers: { 'content-type': 'application/problem+json' } })
    }
    return fetch(request)
  }
}

export function getDevRawFetch(): typeof fetch | undefined {
  const store = getDevDataStore()
  if (!store) return undefined
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init)
    const response = store.handleRawRequest(request)
    if (response) return response
    if (isDemoDataMode()) return new Response('Offline demo raw handler missing.', { status: 501 })
    return fetch(request)
  }
}
