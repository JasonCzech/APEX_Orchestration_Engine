/**
 * Runs-grid filter model + URLSearchParams (de)serialization.
 *
 * The URL is the single source of truth for the grid's filters (shareable deep
 * links, back/forward safe). Defaults are omitted from the URL so a pristine
 * grid is just `/runs`.
 */

export const RUNS_PAGE_SIZE = 25
/** Backend limit ceiling (GET /v1/pipelines `limit` is 1..100). */
export const RUNS_MAX_LIMIT = 100
/** Backend database-list offset ceiling. */
export const RUNS_MAX_OFFSET = 10_000

/** thread_status values surfaced by GET /v1/pipelines (LangGraph thread statuses). */
export const THREAD_STATUSES = ['idle', 'busy', 'interrupted', 'error'] as const
export type ThreadStatus = (typeof THREAD_STATUSES)[number]

export interface RunsFilters {
  status?: ThreadStatus
  q?: string
  project?: string
  limit: number
  offset: number
}

export const DEFAULT_RUNS_FILTERS: RunsFilters = { limit: RUNS_PAGE_SIZE, offset: 0 }

export function isThreadStatus(value: unknown): value is ThreadStatus {
  return typeof value === 'string' && (THREAD_STATUSES as readonly string[]).includes(value)
}

function parseBoundedInt(raw: string | null, fallback: number, min: number, max: number): number {
  if (raw === null || raw.trim() === '') return fallback
  const value = Number(raw)
  if (!Number.isInteger(value)) return fallback
  return Math.min(Math.max(value, min), max)
}

/** Reads filters from the URL; unknown statuses and malformed numbers fall back to defaults. */
export function parseRunsFilters(params: URLSearchParams): RunsFilters {
  const status = params.get('status')
  const q = params.get('q')?.trim()
  const project = params.get('project')?.trim()
  const limit = parseBoundedInt(params.get('limit'), RUNS_PAGE_SIZE, 1, RUNS_MAX_LIMIT)
  return {
    ...(isThreadStatus(status) ? { status } : {}),
    ...(q ? { q } : {}),
    ...(project ? { project } : {}),
    limit,
    offset: parseBoundedInt(params.get('offset'), 0, 0, RUNS_MAX_OFFSET),
  }
}

/** Writes filters to the URL, omitting unset values and defaults. */
export function serializeRunsFilters(filters: RunsFilters): URLSearchParams {
  const params = new URLSearchParams()
  if (filters.status) params.set('status', filters.status)
  if (filters.q) params.set('q', filters.q)
  if (filters.project) params.set('project', filters.project)
  if (filters.limit !== RUNS_PAGE_SIZE) params.set('limit', String(filters.limit))
  if (filters.offset > 0) params.set('offset', String(filters.offset))
  return params
}

/** True when any non-pagination filter is applied (drives the clear-filters affordance). */
export function hasActiveFilters(filters: RunsFilters): boolean {
  return Boolean(filters.status || filters.q || filters.project)
}
