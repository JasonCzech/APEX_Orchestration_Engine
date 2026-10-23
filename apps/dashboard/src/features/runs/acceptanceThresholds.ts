/**
 * Shared rules for judging observed KPIs against the acceptance thresholds an
 * engine applied (F-06). Used by the run's Requested vs observed table and by
 * the runs-compare view, so both say the same thing about the same run.
 */
import type { TestResultSummary } from '@apex/pipeline-events'

/** Throughput-style KPIs are minimums; everything else is a maximum. */
export function isMinimumThreshold(key: string): boolean {
  return /(^|_)(tps|rps|throughput)(_|$)/.test(key)
}

export const ENGINE_LABELS: Record<string, string> = { sim: 'Simulated engine', apex_load: 'APEX Load', loadrunner: 'LoadRunner' }

export function engineLabel(engine: string | null | undefined): string | null {
  return engine ? ENGINE_LABELS[engine] ?? engine : null
}

/** The engine's own pass/fail decision, which threshold views never override. */
export function engineVerdict(summary: TestResultSummary): string {
  const assessed = summary.outcome?.assessment.status
  if (assessed) return ({ passed: 'Passed', failed: 'Failed', inconclusive: 'Inconclusive', not_assessed: 'Not assessed', unavailable: 'Not available' })[assessed]
  return summary.passed === true ? 'Passed' : summary.passed === false ? 'Failed' : 'Not assessed'
}

/**
 * Only the simulator judges these exact KPIs against these thresholds. Real
 * engines assess with their own aggregates, so a per-row verdict could
 * contradict them; show the engine's verdict instead.
 */
export function judgesPerRow(summary: TestResultSummary | null | undefined): boolean {
  return summary?.engine === 'sim' && !summary.outcome
}

/** Met/not met for one KPI, null when either side is missing. */
export function thresholdMet(key: string, limit: number | undefined, observed: number | null | undefined): boolean | null {
  if (limit === undefined || observed === undefined || observed === null) return null
  return isMinimumThreshold(key) ? observed >= limit : observed <= limit
}
