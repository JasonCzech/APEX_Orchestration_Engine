/**
 * /logs filter model + URLSearchParams (de)serialization.
 *
 * The URL carries the *submitted* search (?q&from&to&thread&service&level) so
 * results are shareable and a run can deep-link its logs via ?thread=<id>.
 * Form edits stay local until the explicit [Search] submit commits them.
 */

import type { LogSearchInput } from '@/api/hooks/useLogs'

export const LOGS_PAGE_SIZE = 50
/** Shared provider Page contract caps offsets at 1,000. */
export const LOGS_MAX_OFFSET = 1_000

/** Conventional severity chips; the backend stores levels as plain strings. */
export const LOG_LEVELS = ['ERROR', 'WARN', 'INFO', 'DEBUG'] as const
export type LogLevel = (typeof LOG_LEVELS)[number]

export interface LogsFilters {
  q?: string
  from?: string
  to?: string
  /** thread_id exact-match filter (deep link from a run). */
  thread?: string
  service?: string
  level?: string
}

function parseIso(raw: string | null): string | undefined {
  if (!raw) return undefined
  return Number.isNaN(Date.parse(raw)) ? undefined : raw
}

/** Reads the submitted search from the URL; malformed dates are dropped. */
export function parseLogsFilters(params: URLSearchParams): LogsFilters {
  const q = params.get('q')?.trim()
  const thread = params.get('thread')?.trim()
  const service = params.get('service')?.trim()
  const level = params.get('level')?.trim()
  return {
    ...(q ? { q } : {}),
    ...(parseIso(params.get('from')) ? { from: parseIso(params.get('from')) } : {}),
    ...(parseIso(params.get('to')) ? { to: parseIso(params.get('to')) } : {}),
    ...(thread ? { thread } : {}),
    ...(service ? { service } : {}),
    ...(level ? { level } : {}),
  }
}

/** Writes the submitted search to the URL, omitting unset values. */
export function serializeLogsFilters(filters: LogsFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.q) params.set('q', filters.q)
  if (filters.from) params.set('from', filters.from)
  if (filters.to) params.set('to', filters.to)
  if (filters.thread) params.set('thread', filters.thread)
  if (filters.service) params.set('service', filters.service)
  if (filters.level) params.set('level', filters.level)
  return params
}

/** True when the URL carries any search input (auto-run deep links on mount). */
export function hasLogsFilters(filters: LogsFilters): boolean {
  return Boolean(
    filters.q || filters.from || filters.to || filters.thread || filters.service || filters.level,
  )
}

/**
 * Filters -> POST /v1/logs/search input. service/level/thread become the
 * backend's ANDed exact-match `filters` map ('thread_id' deep-links a run's
 * logs by convention); from/to become the request window (server defaults to
 * the last hour when omitted).
 */
export function buildLogSearchInput(
  filters: LogsFilters,
  limit: number = LOGS_PAGE_SIZE,
  offset = 0,
): LogSearchInput {
  const filterMap: Record<string, string> = {}
  if (filters.service) filterMap.service = filters.service
  if (filters.level) filterMap.level = filters.level
  if (filters.thread) filterMap.thread_id = filters.thread
  return {
    ...(filters.q ? { text: filters.q } : {}),
    filters: filterMap,
    ...(filters.from ? { from: filters.from } : {}),
    ...(filters.to ? { to: filters.to } : {}),
    limit,
    offset,
  }
}

/** status-badge tone per log level (ERROR danger / WARN warning / INFO info / DEBUG muted). */
export function levelTone(level: string): 'danger' | 'warning' | 'info' | 'neutral' {
  switch (level.toUpperCase()) {
    case 'ERROR':
    case 'FATAL':
    case 'CRITICAL':
      return 'danger'
    case 'WARN':
    case 'WARNING':
      return 'warning'
    case 'INFO':
      return 'info'
    default:
      return 'neutral'
  }
}
