import { getDevDataStore } from './controller'

export async function handleDevApexRequest(request: Request): Promise<Response | null> {
  return getDevDataStore()?.handleApexRequest(request) ?? null
}

export function getDevApexFetch(): typeof fetch | undefined {
  if (!getDevDataStore()) return undefined
  return async (input: RequestInfo | URL, init?: RequestInit) => {
    const request = new Request(input, init)
    const response = await handleDevApexRequest(request)
    return response ?? fetch(request)
  }
}
