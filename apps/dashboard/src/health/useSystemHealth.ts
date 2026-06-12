import { useQuery } from '@tanstack/react-query'

import { fetchSystemInfo, type SystemInfo } from '@/api/apexClient'
import { isApiError } from '@/api/errors'
import { queryKeys, STALE_TIMES } from '@/api/queryKeys'

export type ConnectivityStatus = 'unknown' | 'ok' | 'degraded' | 'unreachable'

export const HEALTH_POLL_INTERVAL_MS = 30_000

export interface SystemHealth {
  status: ConnectivityStatus
  systemInfo: SystemInfo | null
}

/**
 * Polls GET /v1/system/info every 30s (paused while the tab is hidden via
 * refetchIntervalInBackground: false). HTTP errors mean the API answered but
 * is unhealthy (degraded); network failures mean unreachable.
 */
export function useSystemHealth(enabled: boolean): SystemHealth {
  const query = useQuery({
    queryKey: queryKeys.system.info(),
    queryFn: fetchSystemInfo,
    enabled,
    refetchInterval: HEALTH_POLL_INTERVAL_MS,
    refetchIntervalInBackground: false,
    staleTime: STALE_TIMES.systemInfo,
  })

  let status: ConnectivityStatus = 'unknown'
  if (query.error) {
    status = isApiError(query.error) ? 'degraded' : 'unreachable'
  } else if (query.data) {
    status = 'ok'
  }

  return { status, systemInfo: query.data ?? null }
}
