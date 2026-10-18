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
import type { LiveStreamViewLike } from './liveTypes'

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
  | 'unknown'

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
      return 'pending'
    default:
      return 'unknown'
  }
}

export function pipelineStatusLabel(status: string | null | undefined): string {
  if (status === 'none') return 'Not selected'
  if (status === 'aborted') return 'Aborted'
  switch (pipelinePhaseVisual(status)) {
    case 'prompt_review':
      return 'Prompt review'
    case 'running':
      return 'Executing'
    case 'results_ready':
      return 'Results ready'
    case 'completed':
      return 'Complete'
    case 'failed':
      return 'Failed'
    case 'skipped':
      return 'Skipped'
    case 'pending':
      return 'Pending'
    default:
      return 'Unknown'
  }
}

export function isPipelinePhaseComplete(status: string | null | undefined): boolean {
  return ['succeeded', 'failed', 'aborted', 'skipped'].includes(status ?? '')
}

/** Only a full snapshot can prove that execution was omitted without imported evidence. */
export function executionWasOmitted(state?: PipelineState): boolean {
  return Boolean(state?.projection_truncated !== true && state?.phases_plan?.length && !state.phases_plan.includes('execution') &&
    !state.phase_results?.execution && !state.external_results && !state.engine_handle)
}

/** A phase strip alone cannot distinguish omission from missing or imported evidence. */
export function executionResultLabel(run: { execution_result?: string | null; phase_strip?: PipelineSummary['phase_strip'] }, state?: PipelineState): string {
  const executionStatus = state?.phase_results?.execution?.status ?? run.phase_strip?.find(entry => entry.phase === 'execution')?.status
  if (executionStatus !== 'succeeded') return 'Not available'
  const summary = state?.phase_results?.execution?.test_summary
  const result = run.execution_result ?? summary?.outcome?.assessment?.status ?? (summary?.passed === true ? 'passed' : summary?.passed === false ? 'failed' : null)
  if (result === 'inconclusive') return 'Inconclusive'
  if (result === 'not_assessed') return 'Not assessed'
  if (result === 'passed') return 'Passed'
  if (result === 'failed') return 'Failed'
  return 'Not available'
}

export function orchestrationStatusLabel(status: string | null | undefined, strip?: PipelineSummary['phase_strip']): string {
  if (status === 'idle' && strip) {
    const selected = strip.filter(entry => entry.status !== 'none' && entry.status !== 'unknown')
    if (selected.some(entry => entry.status === 'aborted')) return 'Stopped by operator'
    if (selected.some(entry => entry.status === 'failed')) return 'Orchestration failed'
    if (selected.length && selected.every(entry => ['succeeded', 'skipped'].includes(entry.status))) return 'Orchestration complete'
    return 'Orchestration stopped'
  }
  return ({ busy: 'Running', interrupted: 'Awaiting review', idle: 'Not running', error: 'Orchestration failed' } as Record<string, string>)[status ?? ''] ?? 'Unknown'
}

export function orchestrationTone(status: string | null | undefined, strip?: PipelineSummary['phase_strip']): string {
  const label = orchestrationStatusLabel(status, strip)
  return label === 'Orchestration complete' ? 'success' : label === 'Orchestration failed' ? 'danger'
    : status === 'busy' ? 'accent' : status === 'interrupted' ? 'warning' : 'neutral'
}

export function gateLabel(kind: string | null | undefined): string {
  return ({ prompt_review: 'Review prompts', phase_review: 'Review phase results', engine_provision_retry: 'Recover engine provisioning' } as Record<string, string>)[kind ?? ''] ?? 'Review required'
}

/** Shared snapshot projection for every detail/compare phase display. */
export function normalizedPhases(
  state: PipelineState,
  fallback: PipelineSummary['phase_strip'] = [],
  useFallback = false,
  context?: Pick<PipelineSummary, 'thread_status' | 'current_phase' | 'pending_gate'>,
) {
  const plan = state.phases_plan?.every(isPhaseName) ? state.phases_plan : undefined
  return PHASE_NAMES.map((phase) => {
    if (plan && !plan.includes(phase)) return { phase, status: 'none', attempt: undefined }
    const entry = state.phase_results?.[phase]
    // useThreadState validates fields independently after schema drift. An
    // unrelated invalid field must not replace a valid status with stale data.
    const gate = context?.thread_status === 'interrupted' ? context.pending_gate : null
    if (gate?.phase === phase && ['prompt_review', 'phase_review'].includes(gate.kind ?? '') && (!entry?.status || !isPipelinePhaseComplete(entry.status))) {
      return { phase, status: gate.kind === 'prompt_review' ? 'awaiting_prompt_review' : 'awaiting_output_review', attempt: entry?.attempt }
    }
    if (context?.thread_status === 'error' && context.current_phase === phase && (!entry?.status || !isPipelinePhaseComplete(entry.status))) {
      return { phase, status: state.run_aborted ? 'aborted' : 'failed', attempt: entry?.attempt }
    }
    if (entry?.status) return { phase, status: entry.status, attempt: entry.attempt }
    const previous = useFallback ? fallback.find((item) => item.phase === phase) : undefined
    return {
      phase,
      status: plan?.includes(phase) && (!previous?.status || previous.status === 'none')
        ? 'pending' : previous?.status ?? (plan ? 'none' : 'unknown'),
      attempt: previous?.attempt ?? undefined,
    }
  })
}

/** A running child subgraph may not have committed to its parent checkpoint yet.
 * Preserve that same-run observation for display only; accepted outcomes, review
 * gates, attempts and the canonical query cache remain snapshot-owned. */
export function withLiveRunningPhase(
  strip: ReturnType<typeof normalizedPhases>,
  context: Pick<PipelineSummary, 'thread_status' | 'current_phase' | 'pending_gate'>,
  live: { runId: string | null; stream: Pick<LiveStreamViewLike, 'status' | 'phaseProgress'> },
) {
  if (context.thread_status !== 'busy' || context.pending_gate || !live.runId ||
    !['live', 'reconnecting'].includes(live.stream.status)) return strip
  if (strip.some(item => ['running', 'awaiting_prompt_review', 'awaiting_output_review'].includes(item.status))) return strip
  const observations = Object.entries(live.stream.phaseProgress ?? {})
  if (observations.some(([, progress]) => progress && ['awaiting_prompt_review', 'awaiting_output_review'].includes(progress.status ?? ''))) return strip
  const running = observations.filter(([, progress]) => progress?.status === 'running')
  if (running.length !== 1) return strip
  const [phase, observation] = running[0]!
  if (!isPhaseName(phase) || !observation || !Number.isInteger(observation.attempt) || (observation.attempt ?? 0) < 1) return strip
  if (isPhaseName(context.current_phase) && PHASE_NAMES.indexOf(context.current_phase) > PHASE_NAMES.indexOf(phase)) return strip
  return strip.map(item => item.phase === phase && item.status === 'pending' && item.attempt === observation.attempt
    ? { ...item, status: 'running' }
    : item)
}

export function phaseProgress(strip: PipelineSummary['phase_strip']) {
  const included = strip.filter((entry) => entry.status !== 'none' && entry.status !== 'unknown')
  return {
    completed: included.filter(entry => isPipelinePhaseComplete(entry.status)).length,
    succeeded: included.filter(entry => entry.status === 'succeeded').length,
    failed: included.filter(entry => entry.status === 'failed').length,
    aborted: included.filter(entry => entry.status === 'aborted').length,
    skipped: included.filter(entry => entry.status === 'skipped').length,
    total: included.length,
    planRecorded: included.length > 0,
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
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0) return '—'
  if (seconds < 0.9995) return `${Math.round(seconds * 1000)}ms`
  const tenths = Math.round(seconds * 10) / 10
  if (tenths < 60) return `${Number.isInteger(tenths) ? tenths.toFixed(0) : tenths.toFixed(1)}s`
  const wholeSeconds = Math.round(seconds)
  if (wholeSeconds < 3600) {
    const minutes = Math.floor(wholeSeconds / 60)
    const rest = wholeSeconds % 60
    return rest > 0 ? `${minutes}m ${rest}s` : `${minutes}m`
  }
  const wholeMinutes = Math.round(seconds / 60)
  const hours = Math.floor(wholeMinutes / 60)
  const minutes = wholeMinutes % 60
  return minutes > 0 ? `${hours}h ${minutes}m` : `${hours}h`
}

export { formatTimestamp } from '@/utils/time'

export function phaseProgressLabel(progress: ReturnType<typeof phaseProgress>): string {
  if (!progress.planRecorded) return 'Phase plan not recorded for this run'
  return `${progress.succeeded}/${progress.total} phases succeeded${progress.failed ? ` · ${progress.failed} failed` : ''}${progress.aborted ? ` · ${progress.aborted} aborted` : ''}${progress.skipped ? ` · ${progress.skipped} skipped` : ''}`
}

export function toolCallTone(status: string | null | undefined): StatusTone {
  if (['ok', 'success', 'succeeded', 'completed'].includes(status ?? '')) return 'success'
  if (['error', 'failed', 'timeout', 'denied', 'cancelled', 'aborted'].includes(status ?? '')) return 'danger'
  if (['running', 'started', 'pending'].includes(status ?? '')) return 'accent'
  return 'neutral'
}

/** Kept as an alias for existing consumers: one phase vocabulary everywhere. */
export const statusLabel = pipelineStatusLabel
