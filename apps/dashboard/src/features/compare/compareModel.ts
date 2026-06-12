/**
 * Pure helpers for the runs-compare view (D8): ?ids= parsing, per-row duration
 * divergence, and KPI best/worst ranking. No React, no fetching — unit-testable
 * and shared by ComparePage + the runs-grid selection affordance.
 */
import type { TestResultSummary } from '@apex/pipeline-events'

/** The compare view renders 2–4 side-by-side run columns. */
export const MAX_COMPARE_RUNS = 4

/** A row's slowest duration is highlighted when it exceeds 1.5x the fastest. */
export const SLOW_HIGHLIGHT_RATIO = 1.5

/**
 * Parse the ?ids= value into thread ids: comma-split, trimmed, de-duplicated,
 * capped at MAX_COMPARE_RUNS (extra ids are ignored, not an error).
 */
export function parseCompareIds(raw: string | null): string[] {
  if (!raw) return []
  const ids: string[] = []
  for (const piece of raw.split(',')) {
    const id = piece.trim()
    if (id && !ids.includes(id)) ids.push(id)
    if (ids.length === MAX_COMPARE_RUNS) break
  }
  return ids
}

/** status-badge tone per LangGraph thread_status (mirrors the runs grid). */
export function threadStatusTone(status: string | null | undefined): string {
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

/**
 * Index of the slowest duration in a row when it diverges (> RATIO x fastest);
 * -1 when fewer than two durations are present or the spread is within bounds.
 */
export function slowestIndex(durations: ReadonlyArray<number | null | undefined>): number {
  const present = durations
    .map((value, index) => ({ value, index }))
    .filter(
      (entry): entry is { value: number; index: number } =>
        typeof entry.value === 'number' && Number.isFinite(entry.value) && entry.value >= 0,
    )
  if (present.length < 2) return -1
  let fastest = present[0]!
  let slowest = present[0]!
  for (const entry of present) {
    if (entry.value < fastest.value) fastest = entry
    if (entry.value > slowest.value) slowest = entry
  }
  return slowest.value > fastest.value * SLOW_HIGHLIGHT_RATIO ? slowest.index : -1
}

export type KpiDirection = 'higher' | 'lower'

export interface CompareKpiDef {
  /** Key into TestResultSummary.kpis (normalized engine KPI names, M5). */
  key: string
  label: string
  better: KpiDirection
  format: (value: number) => string
}

/** Engine KPI rows compared across runs (same set the live EngineStrip pills show). */
export const COMPARE_KPIS: CompareKpiDef[] = [
  {
    key: 'tps_avg',
    label: 'TPS avg',
    better: 'higher',
    format: (v) => (Number.isInteger(v) ? String(v) : v.toFixed(1)),
  },
  { key: 'p95_ms', label: 'p95', better: 'lower', format: (v) => `${Math.round(v)} ms` },
  {
    key: 'error_rate',
    label: 'Error rate',
    better: 'lower',
    format: (v) => `${(v * 100).toFixed(2)}%`,
  },
  { key: 'vusers_peak', label: 'VUsers peak', better: 'higher', format: (v) => String(Math.round(v)) },
]

/** Numeric KPI from a test_summary, null when absent/non-numeric. */
export function kpiValue(summary: TestResultSummary | null | undefined, key: string): number | null {
  const value = summary?.kpis?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export interface BestWorst {
  best: number
  worst: number
}

/**
 * Indices of the best and worst value in a KPI row, honoring the metric's
 * direction. Null when fewer than two values are present or all present
 * values tie (nothing meaningful to tint).
 */
export function bestWorst(
  values: ReadonlyArray<number | null>,
  better: KpiDirection,
): BestWorst | null {
  const present = values
    .map((value, index) => ({ value, index }))
    .filter((entry): entry is { value: number; index: number } => entry.value !== null)
  if (present.length < 2) return null
  let lowest = present[0]!
  let highest = present[0]!
  for (const entry of present) {
    if (entry.value < lowest.value) lowest = entry
    if (entry.value > highest.value) highest = entry
  }
  if (lowest.value === highest.value) return null
  return better === 'higher'
    ? { best: highest.index, worst: lowest.index }
    : { best: lowest.index, worst: highest.index }
}
