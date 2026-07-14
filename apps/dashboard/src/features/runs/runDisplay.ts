/**
 * Pure display helpers shared by the run-detail surfaces (rail, workspace,
 * timeline). Phase identity comes from @apex/pipeline-events — never redeclared.
 */
import {
  PHASE_NAMES,
  type PhaseName,
  type PhaseResultEntry,
  type PipelineState,
} from '@apex/pipeline-events'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import type { PipelineSummary } from '@/api/hooks/usePipelines'

export const PHASE_LABELS: Record<PhaseName, string> = {
  story_analysis: 'Story Analysis',
  test_planning: 'Test Planning',
  env_triage: 'Env Triage',
  script_scenario: 'Script & Scenario',
  execution: 'Execution',
  reporting: 'Reporting',
  postmortem: 'Postmortem',
}

export function isPhaseName(value: unknown): value is PhaseName {
  return typeof value === 'string' && (PHASE_NAMES as readonly string[]).includes(value)
}

export function phaseEntry(state: PipelineState, phase: PhaseName): PhaseResultEntry | undefined {
  return state.phase_results?.[phase]
}

/**
 * Redirect target for /runs/:threadId — the facade's current_phase when valid,
 * else the first canonical phase with a result, else the first planned phase.
 */
export function targetPhaseFor(detail: PipelineDetail, state: PipelineState): PhaseName {
  if (isPhaseName(detail.current_phase)) return detail.current_phase
  const withResult = PHASE_NAMES.find((phase) => state.phase_results?.[phase] !== undefined)
  if (withResult) return withResult
  const planned = state.phases_plan?.[0]
  if (isPhaseName(planned)) return planned
  return PHASE_NAMES[0]
}

export type StatusTone = 'success' | 'danger' | 'warning' | 'accent' | 'neutral'

export type PipelinePhaseVisual =
  | 'pending'
  | 'prompt_review'
  | 'running'
  | 'results_ready'
  | 'completed'
  | 'failed'
  | 'skipped'

export interface StatusVisual {
  tone: StatusTone
  /** True while the phase is in-flight — drives the pulsing status dot. */
  active: boolean
}

export function statusVisual(status: string | null | undefined): StatusVisual {
  switch (status) {
    case 'succeeded':
      return { tone: 'success', active: false }
    case 'failed':
    case 'aborted':
      return { tone: 'danger', active: false }
    case 'running':
      return { tone: 'accent', active: true }
    case 'awaiting_prompt_review':
    case 'awaiting_output_review':
      return { tone: 'warning', active: true }
    case 'skipped':
    default:
      return { tone: 'neutral', active: false }
  }
}

export function pipelinePhaseVisual(status: string | null | undefined): PipelinePhaseVisual {
  switch (status) {
    case 'succeeded':
      return 'completed'
    case 'failed':
    case 'aborted':
      return 'failed'
    case 'running':
      return 'running'
    case 'awaiting_prompt_review':
      return 'prompt_review'
    case 'awaiting_output_review':
      return 'results_ready'
    case 'skipped':
      return 'skipped'
    case 'pending':
    default:
      return 'pending'
  }
}

export function pipelineStatusLabel(status: string | null | undefined): string {
  switch (pipelinePhaseVisual(status)) {
    case 'prompt_review':
      return 'Prompt Review'
    case 'running':
      return 'Executing'
    case 'results_ready':
      return 'Results Ready'
    case 'completed':
      return 'Complete'
    case 'failed':
      return 'Failed'
    case 'skipped':
      return 'Skipped'
    case 'pending':
    default:
      return 'Pending'
  }
}

export function isPipelinePhaseComplete(status: string | null | undefined): boolean {
  return ['succeeded', 'failed', 'aborted', 'skipped'].includes(status ?? '')
}

export type PipelineVerdict = 'GO' | 'Conditional' | 'NO-GO' | '—'

/**
 * Conservative fleet verdict derived from the outcomes the list facade
 * actually exposes. Thread `idle` only means execution stopped; it is GO only
 * when at least one selected phase completed successfully (or was skipped)
 * and no phase failed, aborted, or remained in flight.
 */
export function pipelineVerdict(
  run: Pick<PipelineSummary, 'phase_strip' | 'thread_status'>,
): PipelineVerdict {
  const statuses = run.phase_strip.map((entry) => entry.status)
  if (statuses.some((status) => status === 'failed' || status === 'aborted')) return 'NO-GO'
  switch (run.thread_status) {
    case 'interrupted':
      return 'Conditional'
    case 'error':
      return 'NO-GO'
    case 'idle':
      if (
        statuses.some((status) =>
          ['running', 'awaiting_prompt_review', 'awaiting_output_review'].includes(status),
        )
      ) {
        return 'NO-GO'
      }
      return statuses.some((status) => status === 'succeeded' || status === 'skipped') ? 'GO' : '—'
    default:
      return '—'
  }
}

/** CSS color custom property per tone (status dots use currentColor). */
export const TONE_COLOR_VAR: Record<StatusTone, string> = {
  success: 'var(--success)',
  danger: 'var(--danger)',
  warning: 'var(--warning)',
  accent: 'var(--accent)',
  neutral: 'var(--text-muted)',
}

/** Compact duration: 850ms / 4.2s / 1m 15s / 1h 4m. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return '—'
  if (seconds < 1) return `${Math.round(seconds * 1000)}ms`
  if (seconds < 60) {
    const value = Math.round(seconds * 10) / 10
    return `${Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}s`
  }
  if (seconds < 3600) {
    const minutes = Math.floor(seconds / 60)
    const rest = Math.round(seconds % 60)
    return rest > 0 ? `${minutes}m ${rest}s` : `${minutes}m`
  }
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.round((seconds % 3600) / 60)
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`
}

/** Locale-aware short timestamp; em dash when absent/unparseable. */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

/** Human label for a phase status ("awaiting_prompt_review" -> "awaiting prompt review"). */
export function statusLabel(status: string | null | undefined): string {
  return (status ?? 'pending').replaceAll('_', ' ')
}
