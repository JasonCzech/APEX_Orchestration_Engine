import { QueryClient } from '@tanstack/react-query'

import { isApiError } from './errors'

/**
 * Query defaults per plan Part 2: retry transient failures twice, never retry
 * 4xx (client errors are deterministic); stale times are applied per-domain
 * via STALE_TIMES in queryKeys.ts. refetchOnWindowFocus stays off — liveness
 * comes from explicit polls (façade 15s, health 30s) and per-run SSE streams.
 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          if (isApiError(error) && error.status >= 400 && error.status < 500) return false
          return failureCount < 2
        },
        staleTime: 0,
        refetchOnWindowFocus: false,
      },
      mutations: {
        retry: false,
      },
    },
  })
}
