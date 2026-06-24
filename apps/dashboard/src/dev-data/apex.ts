import { getDevDataStore } from './controller'

export async function handleDevApexRequest(request: Request): Promise<Response | null> {
  return getDevDataStore()?.handleApexRequest(request) ?? null
}

export function getDevApexFetch(): ((input: Request) => Promise<Response>) | undefined {
  if (!getDevDataStore()) return undefined
  return async (request: Request) => {
    const response = await handleDevApexRequest(request)
    return response ?? fetch(request)
  }
}

