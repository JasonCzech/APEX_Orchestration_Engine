/**
 * /analytics filter model + URLSearchParams (de)serialization (runs-grid
 * pattern: the URL is the single source of truth — shareable deep links,
 * back/forward safe; defaults are omitted so a pristine screen is /analytics).
 */

import { DAY_MS, HOUR_MS } from '@/components/controls/timeWindow'

export const BUCKETS = ['hour', 'day'] as const
export type Bucket = (typeof BUCKETS)[number]

export interface AnalyticsFilters {
  /** ISO window start; omitted = server default (`to` minus 7 days). */
  from?: string
  /** ISO window end; omitted = server default (now). */
  to?: string
  /** Explicit histogram bucket; omitted = auto (see effectiveBucket). */
  bucket?: Bucket
  project?: string
}

export function isBucket(value: unknown): value is Bucket {
  return typeof value === 'string' && (BUCKETS as readonly string[]).includes(value)
}

function parseIso(raw: string | null): string | undefined {
  if (!raw) return undefined
  return Number.isNaN(Date.parse(raw)) ? undefined : raw
}

/** Reads filters from the URL; malformed dates and unknown buckets fall back to defaults. */
export function parseAnalyticsFilters(params: URLSearchParams): AnalyticsFilters {
  const bucket = params.get('bucket')
  const project = params.get('project')?.trim()
  return {
    ...(parseIso(params.get('from')) ? { from: parseIso(params.get('from')) } : {}),
    ...(parseIso(params.get('to')) ? { to: parseIso(params.get('to')) } : {}),
    ...(isBucket(bucket) ? { bucket } : {}),
    ...(project ? { project } : {}),
  }
}

/** Writes filters to the URL, omitting unset values. */
export function serializeAnalyticsFilters(filters: AnalyticsFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.from) params.set('from', filters.from)
  if (filters.to) params.set('to', filters.to)
  if (filters.bucket) params.set('bucket', filters.bucket)
  if (filters.project) params.set('project', filters.project)
  return params
}

/** Auto-bucket cutover: windows spanning at most 48h chart hourly. */
export const AUTO_HOUR_MAX_MS = 48 * HOUR_MS

/** Server default window span when from/to are omitted (= `to` minus 7 days). */
const DEFAULT_SPAN_MS = 7 * DAY_MS

/**
 * Bucket sent to the API: the explicit ?bucket when present, otherwise auto —
 * `hour` when the (explicit or server-default 7d) window spans <= 48h, else
 * `day`. 48h (not 24h) so a "yesterday + today" custom window still reads as
 * an hourly histogram instead of two day bars.
 */
export function effectiveBucket(filters: AnalyticsFilters, now: number = Date.now()): Bucket {
  if (filters.bucket) return filters.bucket
  const to = filters.to ? Date.parse(filters.to) : now
  const from = filters.from ? Date.parse(filters.from) : to - DEFAULT_SPAN_MS
  return to - from <= AUTO_HOUR_MAX_MS ? 'hour' : 'day'
}

/** True when any filter is applied (drives the clear-filters affordance). */
export function hasAnalyticsFilters(filters: AnalyticsFilters): boolean {
  return Boolean(filters.from || filters.to || filters.bucket || filters.project)
}
