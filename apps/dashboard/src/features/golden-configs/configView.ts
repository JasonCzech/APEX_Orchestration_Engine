/**
 * configView.ts — pure read-side derivations over an assistant's pinned
 * config.configurable bundle for the /golden-configs screens.
 *
 * Contract mirrored (verify on drift): src/apex/graphs/pipeline/configurable.py
 * PipelineConfigurable {project_id, app_id, environment_id, engine, phases? |
 * start_phase/stop_after, gates{phase:{prompt_review,output_review}} (missing
 * phase => GatePolicy() = both GATED), prompt_overrides{key:{content?,
 * version_id?}}, limits{max_revise_loops, max_dialogue_turns, poll_interval_s,
 * poll_timeout_s}}.
 *
 * Deliberately self-contained: nothing is imported from the wizard's
 * wizardState.ts (another team's file) — these are read-only views, not the
 * launch builder.
 */
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

export type GateModeView = 'gated' | 'auto'

export interface GatePairView {
  prompt_review: GateModeView
  output_review: GateModeView
}

export type GateMatrixView = Record<PhaseName, GatePairView>

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isPhaseName(value: unknown): value is PhaseName {
  return typeof value === 'string' && (PHASE_NAMES as readonly string[]).includes(value)
}

/** Backend default: anything that isn't explicitly "auto" reviews as gated. */
function gateModeOf(value: unknown): GateModeView {
  return value === 'auto' ? 'auto' : 'gated'
}

/** configurable.gates -> an explicit 7x2 view (missing phases default gated). */
export function gateMatrixView(gates: unknown): GateMatrixView {
  const source = isRecord(gates) ? gates : {}
  return Object.fromEntries(
    PHASE_NAMES.map((phase) => {
      const entry = isRecord(source[phase]) ? source[phase] : {}
      return [
        phase,
        {
          prompt_review: gateModeOf(entry['prompt_review']),
          output_review: gateModeOf(entry['output_review']),
        },
      ]
    }),
  ) as GateMatrixView
}

export type GatesModeLabel = 'all gated' | 'all auto' | 'custom gates'

/** Inferred gates-mode chip: uniform matrices collapse to all gated/all auto. */
export function inferGatesMode(gates: unknown): GatesModeLabel {
  const matrix = gateMatrixView(gates)
  const modes = PHASE_NAMES.flatMap((phase) => [
    matrix[phase].prompt_review,
    matrix[phase].output_review,
  ])
  if (modes.every((mode) => mode === 'gated')) return 'all gated'
  if (modes.every((mode) => mode === 'auto')) return 'all auto'
  return 'custom gates'
}

/**
 * The pinned phase plan in canonical order. Mirrors the backend resolution:
 * an explicit phases list wins; otherwise the start_phase/stop_after range;
 * default = all 7.
 */
export function selectedPhasesView(configurable: Record<string, unknown>): PhaseName[] {
  const phases = configurable['phases']
  if (Array.isArray(phases)) {
    const requested = new Set(phases.filter(isPhaseName))
    if (requested.size > 0) return PHASE_NAMES.filter((phase) => requested.has(phase))
  }
  const start = isPhaseName(configurable['start_phase'])
    ? PHASE_NAMES.indexOf(configurable['start_phase'])
    : 0
  const stop = isPhaseName(configurable['stop_after'])
    ? PHASE_NAMES.indexOf(configurable['stop_after'])
    : PHASE_NAMES.length - 1
  if (start > stop) return []
  return PHASE_NAMES.slice(start, stop + 1)
}

export interface PromptPinView {
  key: string
  /** content = inline override text; version = pinned catalog version id. */
  kind: 'content' | 'version' | 'empty'
  detail: string | null
}

/** configurable.prompt_overrides -> a stable, sorted pin list. */
export function promptPinsView(configurable: Record<string, unknown>): PromptPinView[] {
  const overrides = configurable['prompt_overrides']
  if (!isRecord(overrides)) return []
  return Object.keys(overrides)
    .sort()
    .map((key) => {
      const entry = overrides[key]
      if (isRecord(entry)) {
        if (typeof entry['content'] === 'string' && entry['content'].length > 0) {
          return { key, kind: 'content' as const, detail: entry['content'] }
        }
        if (typeof entry['version_id'] === 'string' && entry['version_id'].length > 0) {
          return { key, kind: 'version' as const, detail: entry['version_id'] }
        }
      }
      return { key, kind: 'empty' as const, detail: null }
    })
}

export interface ScopeView {
  project: string | null
  app: string | null
  environment: string | null
}

export function scopeView(configurable: Record<string, unknown>): ScopeView {
  const str = (value: unknown) => (typeof value === 'string' && value.length > 0 ? value : null)
  return {
    project: str(configurable['project_id']),
    app: str(configurable['app_id']),
    environment: str(configurable['environment_id']),
  }
}

/** Backend Limits defaults (configurable.py Limits model). */
export const LIMIT_DEFAULTS = {
  max_revise_loops: 3,
  max_dialogue_turns: 20,
  poll_interval_s: 5,
  poll_timeout_s: 14_400,
} as const

export type LimitKey = keyof typeof LIMIT_DEFAULTS

export const LIMIT_LABELS: Record<LimitKey, string> = {
  max_revise_loops: 'Max revise loops',
  max_dialogue_turns: 'Max dialogue turns',
  poll_interval_s: 'Poll interval (s)',
  poll_timeout_s: 'Poll timeout (s)',
}

export interface LimitView {
  key: LimitKey
  label: string
  value: number
  /** True when the bundle pins the value (vs the backend default). */
  pinned: boolean
}

export function limitsView(configurable: Record<string, unknown>): LimitView[] {
  const limits = isRecord(configurable['limits']) ? configurable['limits'] : {}
  return (Object.keys(LIMIT_DEFAULTS) as LimitKey[]).map((key) => {
    const pinned = typeof limits[key] === 'number'
    return {
      key,
      label: LIMIT_LABELS[key],
      value: pinned ? (limits[key] as number) : LIMIT_DEFAULTS[key],
      pinned,
    }
  })
}

const ENGINE_LABELS: Record<string, string> = {
  sim: 'Simulated',
  apex_load: 'APEX Load',
  loadrunner: 'LoadRunner',
}

/** The pinned engine id ("sim" when the bundle pins nothing — backend default). */
export function engineOf(configurable: Record<string, unknown>): string {
  const engine = configurable['engine']
  return typeof engine === 'string' && engine.length > 0 ? engine : 'sim'
}

export function engineLabel(engine: string): string {
  return ENGINE_LABELS[engine] ?? engine
}

export interface ConfigSummary {
  engine: string
  gatesMode: GatesModeLabel
  phaseCount: number
  promptPins: number
}

/** The four list-card summary chips. */
export function summarizeConfigurable(configurable: Record<string, unknown>): ConfigSummary {
  return {
    engine: engineOf(configurable),
    gatesMode: inferGatesMode(configurable['gates']),
    phaseCount: selectedPhasesView(configurable).length,
    promptPins: promptPinsView(configurable).length,
  }
}

export function phaseLabel(phase: PhaseName): string {
  return phase.replaceAll('_', ' ')
}

export type JsonParseResult =
  | { ok: true; value: Record<string, unknown> }
  | { ok: false; message: string }

/** Edit-mode validation: the configurable must parse to a JSON object. */
export function parseConfigurableJson(text: string): JsonParseResult {
  try {
    const parsed: unknown = JSON.parse(text)
    if (!isRecord(parsed)) {
      return { ok: false, message: 'Configurable must be a JSON object' }
    }
    return { ok: true, value: parsed }
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? `Invalid JSON: ${error.message}` : 'Invalid JSON',
    }
  }
}
