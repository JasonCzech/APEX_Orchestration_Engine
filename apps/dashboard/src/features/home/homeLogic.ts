/**
 * Home dashboard derivations (plan UX 1.5 — attention-first overview).
 *
 * Pure slices over ONE unfiltered fleet snapshot from usePipelines: the page
 * derives the failures rail, the active-runs grid and the recent table from a
 * single poll. Pending gates come from useApprovalsInbox, which shares the
 * sidebar badge's cache entry (same fixed filter), so the Home screen adds
 * exactly one pipelines request beyond what the shell already polls.
 */
import type { PipelineSummary } from '@/api/hooks/usePipelines'
import { pipelineVerdict } from '@/features/runs/runDisplay'

/** One-page fleet scan — enough headroom for the grid/table/failure slices. */
export const HOME_FLEET_LIMIT = 50
/** Fixed filter object so the query key stays canonical across renders. */
export const HOME_FLEET_FILTER = { limit: HOME_FLEET_LIMIT } as const

/** Oldest pending gates surfaced in the attention rail. */
export const ATTENTION_GATES_LIMIT = 5
/** Most recent failed runs surfaced in the attention rail. */
export const ATTENTION_FAILURES_LIMIT = 5
/** Rows in the recent-runs table. */
export const RECENT_RUNS_LIMIT = 8
/** Resumable drafts shown in the side panel. */
export const PANEL_DRAFTS_LIMIT = 5
const EM_DASH = '—'

const ACTIVE_STATUSES = new Set(['busy', 'interrupted'])

/** updated_at desc (nulls last), thread_id asc tiebreak — stable across polls. */
export function byUpdatedDesc(a: PipelineSummary, b: PipelineSummary): number {
  const ua = a.updated_at ?? null
  const ub = b.updated_at ?? null
  if (ua === ub) return a.thread_id.localeCompare(b.thread_id)
  if (ua === null) return 1
  if (ub === null) return -1
  return ub.localeCompare(ua)
}

/** Busy/interrupted threads for the active grid, most recently updated first. */
export function activeRuns(items: PipelineSummary[]): PipelineSummary[] {
  return items
    .filter((run) => ACTIVE_STATUSES.has(run.thread_status ?? ''))
    .sort(byUpdatedDesc)
}

/** Recent conservative NO-GO threads for the attention rail and metrics. */
export function failedRuns(
  items: PipelineSummary[],
  limit: number = ATTENTION_FAILURES_LIMIT,
): PipelineSummary[] {
  return items
    .filter((run) => pipelineVerdict(run) === 'NO-GO')
    .sort(byUpdatedDesc)
    .slice(0, limit)
}

/** Last n runs by updated for the recent table. */
export function recentRuns(
  items: PipelineSummary[],
  limit: number = RECENT_RUNS_LIMIT,
): PipelineSummary[] {
  return [...items].sort(byUpdatedDesc).slice(0, limit)
}

/** status-badge tone per LangGraph thread_status (RunsListPage convention). */
export function statusTone(status: string | null | undefined): string {
  switch (status) {
    case 'busy':
      return 'accent'
    case 'interrupted':
      return 'warning'
    case 'error':
      return 'danger'
    case 'idle':
      return 'success'
    default:
      return 'neutral'
  }
}

/** "12.5%" or an em dash when the window has no events. */
export function errorRateOf(
  totals: { events: number; errors: number } | undefined,
): string {
  if (!totals || totals.events <= 0) return '—'
  return `${((totals.errors / totals.events) * 100).toFixed(1)}%`
}

/** Deep link into the approvals inbox; falls back to the queue without an id. */
export function gateHref(threadId: string, interruptId: string | null | undefined): string {
  return interruptId ? `/approvals/${threadId}/${interruptId}` : '/approvals'
}

export function totalRuns(items: PipelineSummary[]): number {
  return items.length
}

export function goVerdicts(items: PipelineSummary[]): number {
  return items.filter((run) => pipelineVerdict(run) === 'GO').length
}

export function interruptedRuns(items: PipelineSummary[]): number {
  return items.filter((run) => run.thread_status === 'interrupted').length
}

function durationMs(run: PipelineSummary): number | null {
  if (!run.created_at || !run.updated_at) return null
  const created = Date.parse(run.created_at)
  const updated = Date.parse(run.updated_at)
  if (Number.isNaN(created) || Number.isNaN(updated) || updated < created) return null
  return updated - created
}

export function avgDurationLabel(items: PipelineSummary[]): string {
  const durations = items.map(durationMs).filter((value): value is number => value !== null)
  if (durations.length === 0) return EM_DASH
  const average = durations.reduce((sum, value) => sum + value, 0) / durations.length
  const totalMinutes = Math.round(average / 60_000)
  if (totalMinutes < 60) return `${totalMinutes}m`
  const hours = Math.floor(totalMinutes / 60)
  const minutes = totalMinutes % 60
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`
}
