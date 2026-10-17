import type { PipelineSummary } from '@/api/hooks/usePipelines'
import { executionResultLabel, orchestrationStatusLabel, phaseProgress } from './runDisplay'

export type RunSortKey = 'title' | 'project' | 'application' | 'progress' | 'created' | 'elapsed' | 'result' | 'status'
export interface RunSort { key: RunSortKey; direction: 'asc' | 'desc' }

function timestamp(value: string | null | undefined): number | null {
  if (!value) return null
  const result = Date.parse(value)
  return Number.isFinite(result) ? result : null
}

function valueFor(run: PipelineSummary, key: RunSortKey): string | number | null {
  switch (key) {
    case 'title': return run.title || 'Untitled run'
    case 'project': return run.project_id || null
    case 'application': return run.app_id || null
    case 'progress': {
      const progress = phaseProgress(run.phase_strip)
      return progress.total ? progress.succeeded / progress.total : null
    }
    case 'created': return timestamp(run.created_at)
    case 'elapsed': {
      const created = timestamp(run.created_at)
      const updated = timestamp(run.updated_at)
      return created !== null && updated !== null && updated >= created ? updated - created : null
    }
    case 'result': return executionResultLabel(run)
    case 'status': return orchestrationStatusLabel(run.thread_status, run.phase_strip)
  }
}

/** The API does not expose ordering; sort only the loaded page, leaving unavailable values last. */
export function sortRunPage(items: readonly PipelineSummary[], sort: RunSort | null): PipelineSummary[] {
  if (!sort) return [...items]
  return [...items].sort((a, b) => {
    const left = valueFor(a, sort.key)
    const right = valueFor(b, sort.key)
    if (left === null) return right === null ? 0 : 1
    if (right === null) return -1
    const compared = typeof left === 'number' && typeof right === 'number'
      ? left - right
      : String(left).localeCompare(String(right), undefined, { numeric: true, sensitivity: 'base' })
    return sort.direction === 'asc' ? compared : -compared
  })
}
