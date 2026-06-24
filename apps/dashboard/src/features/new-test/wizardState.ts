/**
 * wizardState.ts — the one typed WizardDraft the 6-step wizard edits, plus the
 * pure derivations around it: per-step validation, phase-prerequisite hints,
 * gates-mode -> gate-matrix mapping, and the EXACT launch payload builder.
 *
 * Contracts mirrored here (verify on drift):
 * - configurable shape: src/apex/graphs/pipeline/configurable.py
 *   PipelineConfigurable {project_id, app_id, environment_id, engine, phases?,
 *   gates{phase:{prompt_review,output_review}}, prompt_overrides{"phase/<p>":
 *   {content}, "application/<app_id>": {content}}, pre_execution_context[]}.
 * - PHASE_PREREQUISITES semantics: src/apex/domain/pipeline.py — a prereq is
 *   satisfied if it runs EARLIER IN THE PLAN or already SUCCEEDED ON THE
 *   THREAD. New threads have no prior results, so a missing prereq is a
 *   warning (never a block): the backend plan resolver decides at run time.
 * - All-auto gate matrix: reuses D2's ALL_AUTO_GATES (features/runs/launchRun).
 */
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { ALL_AUTO_GATES } from '@/features/runs/launchRun'

// ── Steps ────────────────────────────────────────────────────────────────────

export const WIZARD_STEPS = [
  'scope',
  'work-items',
  'context',
  'config',
  'prompts',
  'review',
] as const
export type WizardStepId = (typeof WIZARD_STEPS)[number]

export const STEP_LABELS: Record<WizardStepId, string> = {
  scope: 'Scope',
  'work-items': 'Work Items',
  context: 'Context',
  config: 'Config',
  prompts: 'Prompts',
  review: 'Review',
}

export function isWizardStep(value: string | null): value is WizardStepId {
  return value !== null && (WIZARD_STEPS as readonly string[]).includes(value)
}

// ── Draft shape ──────────────────────────────────────────────────────────────

export const ENGINES = ['sim', 'apex_load', 'loadrunner'] as const
export type EngineId = (typeof ENGINES)[number]

export type GateMode = 'gated' | 'auto'
export interface GatePolicy {
  prompt_review: GateMode
  output_review: GateMode
}
export type GatesMode = 'all_gated' | 'all_auto' | 'custom'
export type GateMatrix = Record<PhaseName, GatePolicy>

export interface WizardScope {
  project_id: string
  app_id: string | null
  environment_id: string | null
}

export interface WizardConfig {
  engine: EngineId
  /** null = all 7 phases (canonical order). */
  phases: PhaseName[] | null
  /** UI focus for the Prompts tab; not sent to the backend. */
  prompt_focus_phase: PhaseName | null
  gates_mode: GatesMode
  gates_custom?: GateMatrix
  golden_config_id?: string | null
}

export interface WizardDraft {
  title: string
  request: string
  scope: WizardScope
  work_item_keys: string[]
  document_ids: string[]
  context_summary_ids: string[]
  config: WizardConfig
  prompt_overrides: Record<string, { content: string }>
}

export function emptyDraft(): WizardDraft {
  return {
    title: '',
    request: '',
    scope: { project_id: 'demo', app_id: null, environment_id: null },
    work_item_keys: [],
    document_ids: [],
    context_summary_ids: [],
    config: {
      engine: 'sim',
      phases: null,
      prompt_focus_phase: PHASE_NAMES[0]!,
      gates_mode: 'all_gated',
      golden_config_id: null,
    },
    prompt_overrides: {},
  }
}

// ── Lenient payload parse (drafts payload is free-form JSONB) ────────────────

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === 'string' ? value : fallback
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function isPhaseName(value: unknown): value is PhaseName {
  return typeof value === 'string' && (PHASE_NAMES as readonly string[]).includes(value)
}

function gateModeOr(value: unknown, fallback: GateMode): GateMode {
  return value === 'gated' || value === 'auto' ? value : fallback
}

/** Normalize an unknown gates blob (draft payload or golden config) onto all 7 phases. */
export function normalizeGateMatrix(value: unknown, fallback: GateMode = 'gated'): GateMatrix {
  const source = isRecord(value) ? value : {}
  return Object.fromEntries(
    PHASE_NAMES.map((phase) => {
      const entry = isRecord(source[phase]) ? (source[phase] as Record<string, unknown>) : {}
      return [
        phase,
        {
          prompt_review: gateModeOr(entry['prompt_review'], fallback),
          output_review: gateModeOr(entry['output_review'], fallback),
        },
      ]
    }),
  ) as GateMatrix
}

/** Canonical-order phase subset from an unknown list; null when empty/absent/full. */
export function normalizePhases(value: unknown): PhaseName[] | null {
  if (!Array.isArray(value)) return null
  const requested = new Set(value.filter(isPhaseName))
  if (requested.size === 0 || requested.size === PHASE_NAMES.length) return null
  return PHASE_NAMES.filter((phase) => requested.has(phase))
}

/** Best-effort restore of a server draft payload; unknown shapes fall back per field. */
export function parseDraftPayload(payload: unknown): WizardDraft {
  const base = emptyDraft()
  if (!isRecord(payload)) return base
  const scope = isRecord(payload['scope']) ? payload['scope'] : {}
  const config = isRecord(payload['config']) ? payload['config'] : {}
  const overridesIn = isRecord(payload['prompt_overrides']) ? payload['prompt_overrides'] : {}
  const prompt_overrides: Record<string, { content: string }> = {}
  for (const [key, entry] of Object.entries(overridesIn)) {
    if (isRecord(entry) && typeof entry['content'] === 'string') {
      prompt_overrides[key] = { content: entry['content'] }
    }
  }
  const engine = config['engine']
  const gatesMode = config['gates_mode']
  return {
    title: stringOr(payload['title'], base.title),
    request: stringOr(payload['request'], base.request),
    scope: {
      project_id: stringOr(scope['project_id'], base.scope.project_id),
      app_id: stringOrNull(scope['app_id']),
      environment_id: stringOrNull(scope['environment_id']),
    },
    work_item_keys: stringArray(payload['work_item_keys']),
    document_ids: stringArray(payload['document_ids']),
    context_summary_ids: stringArray(payload['context_summary_ids']),
    config: {
      engine: (ENGINES as readonly string[]).includes(engine as string)
        ? (engine as EngineId)
        : base.config.engine,
      phases: normalizePhases(config['phases']),
      prompt_focus_phase: isPhaseName(config['prompt_focus_phase'])
        ? config['prompt_focus_phase']
        : base.config.prompt_focus_phase,
      gates_mode:
        gatesMode === 'all_gated' || gatesMode === 'all_auto' || gatesMode === 'custom'
          ? gatesMode
          : base.config.gates_mode,
      ...(isRecord(config['gates_custom'])
        ? { gates_custom: normalizeGateMatrix(config['gates_custom']) }
        : {}),
      golden_config_id: stringOrNull(config['golden_config_id']),
    },
    prompt_overrides,
  }
}

// ── Phase plan + prerequisite hints ──────────────────────────────────────────

/** Mirror of apex.domain.pipeline.PHASE_PREREQUISITES (hard upstream requirements). */
export const PHASE_PREREQUISITES: Record<PhaseName, readonly PhaseName[]> = {
  story_analysis: [],
  test_planning: ['story_analysis'],
  env_triage: [],
  script_scenario: ['test_planning'],
  execution: ['script_scenario'],
  reporting: ['execution'],
  postmortem: ['reporting'],
}

/** The run's phase plan in canonical order (null subset = all 7). */
export function selectedPhases(config: WizardConfig): PhaseName[] {
  return config.phases ?? [...PHASE_NAMES]
}

/** Focused Prompts phase, coerced onto the currently selected phase plan. */
export function focusedPromptPhase(config: WizardConfig): PhaseName | null {
  const selected = selectedPhases(config)
  if (config.prompt_focus_phase && selected.includes(config.prompt_focus_phase)) {
    return config.prompt_focus_phase
  }
  return selected[0] ?? null
}

/**
 * Warn-only dependency hints: a selected phase whose prerequisite is NOT
 * earlier in the plan may still run if the prereq already succeeded on the
 * thread — phrasing mirrors the backend semantics on purpose.
 */
export function phaseDependencyHints(selected: readonly PhaseName[]): string[] {
  const inPlan = new Set(selected)
  const hints: string[] = []
  for (const phase of selected) {
    for (const prereq of PHASE_PREREQUISITES[phase]) {
      // Selection is kept in canonical order, so membership implies "earlier in plan".
      if (!inPlan.has(prereq)) {
        hints.push(`${phase} needs ${prereq} earlier in plan or succeeded on thread`)
      }
    }
  }
  return hints
}

// ── Gates mapping ────────────────────────────────────────────────────────────

export function allGatedMatrix(): GateMatrix {
  return normalizeGateMatrix({}, 'gated')
}

/**
 * gates_mode -> the explicit per-phase matrix the backend expects
 * (configurable.gates). Always explicit for all 7 phases — same shape D2's
 * launchRun sends (ALL_AUTO_GATES is imported, not re-derived).
 */
export function gateMatrixOf(config: WizardConfig): GateMatrix {
  switch (config.gates_mode) {
    case 'all_auto':
      return ALL_AUTO_GATES
    case 'custom':
      return config.gates_custom ?? allGatedMatrix()
    case 'all_gated':
      return allGatedMatrix()
  }
}

// ── Validation ───────────────────────────────────────────────────────────────

export function stepIssues(draft: WizardDraft, step: WizardStepId): string[] {
  const issues: string[] = []
  switch (step) {
    case 'scope': {
      if (draft.title.trim().length === 0) issues.push('Title is required')
      if (draft.request.trim().length === 0) issues.push('Request is required')
      if (draft.scope.project_id.trim().length === 0) issues.push('Project is required')
      break
    }
    case 'work-items':
    case 'context':
      break // both fully optional (skip allowed)
    case 'config': {
      if (draft.config.phases !== null && draft.config.phases.length === 0) {
        issues.push('Select at least one phase')
      }
      break
    }
    case 'prompts': {
      for (const [key, override] of Object.entries(draft.prompt_overrides)) {
        if (override.content.trim().length === 0) {
          issues.push(`Override for ${key} is empty — edit it or revert to catalog`)
        }
      }
      break
    }
    case 'review':
      break
  }
  return issues
}

export function isStepValid(draft: WizardDraft, step: WizardStepId): boolean {
  return stepIssues(draft, step).length === 0
}

export interface StepIssue {
  step: WizardStepId
  message: string
}

/** Everything outstanding across steps — the review step's issue list. */
export function allIssues(draft: WizardDraft): StepIssue[] {
  return WIZARD_STEPS.flatMap((step) =>
    stepIssues(draft, step).map((message) => ({ step, message })),
  )
}

// ── Launch payload (the EXACT payload review shows and launch sends) ─────────

export interface LaunchPreview {
  metadata: Record<string, unknown>
  input: { title: string; request: string }
  configurable: Record<string, unknown>
}

/**
 * Wizard context refs -> configurable.pre_execution_context (list[str]):
 * prefixed refs so the backend can tell sources apart.
 */
function preExecutionContext(draft: WizardDraft): string[] {
  return [
    ...draft.work_item_keys.map((key) => `workitem:${key}`),
    ...draft.document_ids.map((id) => `document:${id}`),
    ...draft.context_summary_ids.map((id) => `context:${id}`),
  ]
}

export function buildConfigurable(draft: WizardDraft): Record<string, unknown> {
  const { scope, config } = draft
  const context = preExecutionContext(draft)
  const overrides = Object.entries(draft.prompt_overrides)
  return {
    project_id: scope.project_id.trim(),
    ...(scope.app_id ? { app_id: scope.app_id } : {}),
    ...(scope.environment_id ? { environment_id: scope.environment_id } : {}),
    engine: config.engine,
    ...(config.phases ? { phases: config.phases } : {}),
    gates: gateMatrixOf(config),
    ...(overrides.length > 0
      ? { prompt_overrides: Object.fromEntries(overrides.map(([k, v]) => [k, { content: v.content }])) }
      : {}),
    ...(context.length > 0 ? { pre_execution_context: context } : {}),
  }
}

export function buildLaunchPreview(draft: WizardDraft): LaunchPreview {
  return {
    metadata: {
      project_id: draft.scope.project_id.trim(),
      ...(draft.scope.app_id ? { app_id: draft.scope.app_id } : {}),
      title: draft.title.trim(),
    },
    input: { title: draft.title.trim(), request: draft.request.trim() },
    configurable: buildConfigurable(draft),
  }
}
