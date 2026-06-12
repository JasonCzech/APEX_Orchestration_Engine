/**
 * Pre-flight readiness assessment for phase-subset re-runs (plan Part 2 §4
 * "Phase-independence UX"). Pure logic — no React, no IO.
 *
 * MIRRORS the backend exactly:
 *   src/apex/domain/pipeline.py        PHASE_ORDER, PHASE_PREREQUISITES
 *   src/apex/graphs/pipeline/graph.py  plan_resolver: a prerequisite is
 *     satisfied when it runs EARLIER IN THE SAME PLAN (canonical order means
 *     membership implies "runs earlier") OR has a SUCCEEDED result already on
 *     the thread. Anything else raises at plan resolution server-side.
 *
 * Policy: warn-don't-block. The UI surfaces blockers as danger rows but never
 * disables Start — the server remains the authority.
 */
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { formatRelative } from '@/utils/time'

import { PHASE_LABELS } from './runDisplay'

/** Canonical phase order (apex.domain.pipeline.PHASE_ORDER). */
export const PHASE_ORDER: readonly PhaseName[] = PHASE_NAMES

/** apex.domain.pipeline.PHASE_PREREQUISITES — hard upstream requirements. */
export const PHASE_PREREQUISITES: Record<PhaseName, readonly PhaseName[]> = {
  story_analysis: [],
  test_planning: ['story_analysis'],
  env_triage: [],
  script_scenario: ['test_planning'],
  execution: ['script_scenario'],
  reporting: ['execution'],
  postmortem: ['reporting'],
}

/** Reused upstream results older than this get the amber drift warning. */
export const STALE_AFTER_MS = 3 * 24 * 60 * 60 * 1000

/** Minimal structural slice of a phase_results entry the assessment reads. */
export interface PreflightPhaseEntry {
  status?: string | null
  attempt?: number | null
  ended_at?: string | null
}

export type PreflightPhaseResults = Record<string, PreflightPhaseEntry | undefined>

export type ReadinessLevel = 'ok' | 'reuse' | 'stale' | 'blocked'

export interface ReadinessRow {
  phase: PhaseName
  level: ReadinessLevel
  /** Prerequisite the level refers to (absent when the phase has none). */
  prereq?: PhaseName
  /** Succeeded attempt being reused (reuse/stale rows). */
  attempt?: number
  /** Human age of the reused result, e.g. "2d ago" (reuse/stale rows). */
  age?: string
  message: string
}

export interface PlanAssessment {
  /** One row per selected phase, in canonical order. */
  rows: ReadinessRow[]
  hasBlockers: boolean
}

/** blocked > stale > reuse > ok (worst prerequisite wins for a phase). */
const SEVERITY: Record<ReadinessLevel, number> = { ok: 0, reuse: 1, stale: 2, blocked: 3 }

function assessPrereq(
  phase: PhaseName,
  prereq: PhaseName,
  selectedSet: ReadonlySet<PhaseName>,
  entry: PreflightPhaseEntry | undefined,
  now: number,
): ReadinessRow {
  const prereqLabel = PHASE_LABELS[prereq]
  if (selectedSet.has(prereq)) {
    // Canonical order: a selected prerequisite always runs earlier in the plan.
    return { phase, level: 'ok', prereq, message: `Runs after ${prereqLabel} in this plan.` }
  }
  if (entry?.status === 'succeeded') {
    const attempt = entry.attempt ?? 1
    const endedAtMs = entry.ended_at ? Date.parse(entry.ended_at) : Number.NaN
    const age = Number.isNaN(endedAtMs) ? undefined : formatRelative(entry.ended_at, now)
    if (!Number.isNaN(endedAtMs) && now - endedAtMs > STALE_AFTER_MS) {
      return {
        phase,
        level: 'stale',
        prereq,
        attempt,
        age,
        message: `Reuses ${prereqLabel} from ${age} — environment may have drifted.`,
      }
    }
    return {
      phase,
      level: 'reuse',
      prereq,
      attempt,
      age,
      message: `Will reuse ${prereqLabel} artifacts (attempt ${attempt}, ${age ?? 'age unknown'}).`,
    }
  }
  return {
    phase,
    level: 'blocked',
    prereq,
    message: `Include ${prereqLabel} or it will fail at plan resolution.`,
  }
}

function assessPhase(
  phase: PhaseName,
  selectedSet: ReadonlySet<PhaseName>,
  results: PreflightPhaseResults,
  now: number,
): ReadinessRow {
  const prereqs = PHASE_PREREQUISITES[phase]
  if (prereqs.length === 0) {
    return { phase, level: 'ok', message: 'No prerequisites.' }
  }
  let worst: ReadinessRow | undefined
  for (const prereq of prereqs) {
    const row = assessPrereq(phase, prereq, selectedSet, results[prereq], now)
    if (!worst || SEVERITY[row.level] > SEVERITY[worst.level]) worst = row
  }
  // prereqs is non-empty here, so worst is always set.
  return worst as ReadinessRow
}

/**
 * Per-phase readiness of a candidate plan against the thread's phase_results.
 * Rows come back in canonical order regardless of `selected` ordering.
 */
export function assessPlan(
  selected: readonly PhaseName[],
  phaseResults: PreflightPhaseResults | undefined,
  now: number = Date.now(),
): PlanAssessment {
  const selectedSet = new Set(selected)
  const rows = PHASE_ORDER.filter((phase) => selectedSet.has(phase)).map((phase) =>
    assessPhase(phase, selectedSet, phaseResults ?? {}, now),
  )
  return { rows, hasBlockers: rows.some((row) => row.level === 'blocked') }
}

/** Type guard reused by the selection helpers below. */
function isPhaseName(value: unknown): value is PhaseName {
  return typeof value === 'string' && (PHASE_NAMES as readonly string[]).includes(value)
}

/**
 * "Run from here": the phase itself plus every downstream phase that was in
 * the LAST resolved plan (canonical-order tail ∩ phases_plan).
 */
export function runFromHereSelection(
  phase: PhaseName,
  lastPlan: readonly string[] | undefined,
): PhaseName[] {
  const start = PHASE_ORDER.indexOf(phase)
  const planSet = new Set((lastPlan ?? []).filter(isPhaseName))
  return PHASE_ORDER.filter((p, i) => i === start || (i > start && planSet.has(p)))
}

/** The last resolved plan, filtered to valid phases, in canonical order. */
export function lastPlanSelection(lastPlan: readonly string[] | undefined): PhaseName[] {
  const planSet = new Set((lastPlan ?? []).filter(isPhaseName))
  return PHASE_ORDER.filter((p) => planSet.has(p))
}
