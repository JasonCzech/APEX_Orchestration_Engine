/**
 * Test presets, one model for both kinds:
 * - team/personal presets (APEX `/v1/test-presets`) store wizard *sections*;
 * - organization presets (LangGraph assistants, platform admins) store a
 *   configurable bundle, converted here into the same sections.
 *
 * A section is a slice of the wizard draft. Applying a preset writes its
 * sections into the draft and remembers them as the draft's `preset.baseline`,
 * so the wizard can count changes, reset to the preset, or detach from it.
 */
import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { emptyPromptSelections, parsePromptSelections } from '@/features/prompts/promptCategories'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { parseTestScope } from '@/features/new-test/testScope'
import { effectiveWorkload } from '@/features/new-test/workload'
import {
  ANALYSIS_LABELS,
  ENGINE_CAPTIONS,
  ENGINES,
  isRecord,
  normalizeAnalysis,
  normalizeGateMatrix,
  normalizePhases,
  parseDraftPayload,
  selectedPhases,
  type DraftPresetLink,
  type EngineId,
  type WizardConfig,
  type WizardDraft,
} from '@/features/new-test/wizardState'
import { selectedPhasesView } from '@/features/golden-configs/configView'

export const SECTION_IDS = ['systems', 'execution', 'phases', 'prompts', 'text', 'context'] as const
export type SectionId = (typeof SECTION_IDS)[number]
export type PresetSections = Partial<Record<SectionId, Record<string, unknown>>>
export type PresetKind = 'team' | 'organization'
export type PresetVisibility = 'personal' | 'project' | 'organization'

export const SECTION_LABELS: Record<SectionId, string> = {
  systems: 'Systems under test',
  execution: 'Engine, workload and analysis',
  phases: 'Phase owners and review policy',
  prompts: 'Prompts',
  text: 'Title and request',
  context: 'Tickets and knowledge',
}

/** Sections a saved preset includes unless the person opts out (or in). */
export const DEFAULT_SAVE_SECTIONS: SectionId[] = ['systems', 'execution', 'phases', 'prompts']

/** A preset of either kind, in the shape the pickers, tables and pages need. */
export interface Preset {
  key: string
  kind: PresetKind
  id: string
  name: string
  description: string | null
  /** The primary application it is offered for; null = General (any application). */
  applicationId: string | null
  projectId: string | null
  visibility: PresetVisibility
  version: number | null
  updatedAt: string | null
  updatedBy: string | null
  owner: string | null
  mine: boolean
  editable: boolean
  archived: boolean
  sections: PresetSections
}

export const presetKey = (kind: PresetKind, id: string) => `${kind}:${id}`

/** The preset's page: organization presets keep their original /test-presets/:assistantId route. */
export function presetHref(kind: string, id: string): string {
  return kind === 'organization' ? `/test-presets/${encodeURIComponent(id)}` : `/test-presets/team/${encodeURIComponent(id)}`
}

export function parsePresetKey(value: string | null): { kind: PresetKind; id: string } | null {
  if (!value) return null
  const [kind, ...rest] = value.split(':')
  const id = rest.join(':')
  return (kind === 'team' || kind === 'organization') && id ? { kind, id } : null
}

// ── Sections ↔ draft ─────────────────────────────────────────────────────────

// Systems live in the systems section and prompt choices in the prompts section;
// a test's execution bundle can carry copies of both, which must not ride along.
const BUNDLE_SCOPE_KEYS = ['test_scope', 'test_scope_snapshot', 'project_id', 'app_id', 'environment_id']
const BUNDLE_PROMPT_KEYS = ['prompt_overrides', 'prompt_selections']

function executionBundle(bundle: Record<string, unknown> | null | undefined, withPrompts: boolean): Record<string, unknown> | null {
  if (!bundle) return null
  const dropped = withPrompts ? BUNDLE_SCOPE_KEYS : [...BUNDLE_SCOPE_KEYS, ...BUNDLE_PROMPT_KEYS]
  return Object.fromEntries(Object.entries(bundle).filter(([key]) => !dropped.includes(key)))
}

/** The draft slice each section owns. */
export function sectionsFromDraft(draft: WizardDraft, include: readonly SectionId[] = SECTION_IDS): PresetSections {
  const sections: PresetSections = {}
  for (const id of include) {
    switch (id) {
      case 'systems':
        sections.systems = { scope: draft.scope }
        break
      case 'execution':
        sections.execution = {
          engine: draft.config.engine,
          unsupported_engine: draft.config.unsupported_engine ?? null,
          golden_config_id: draft.config.golden_config_id ?? null,
          golden_configurable: executionBundle(draft.config.golden_configurable, include.includes('prompts')),
          analysis: draft.config.analysis ?? normalizeAnalysis(undefined),
        }
        break
      case 'phases':
        sections.phases = {
          phases: draft.config.phases,
          schema_version: 2,
          migrated_phase_ownership: draft.config.migrated_phase_ownership ?? false,
          gates_mode: draft.config.gates_mode,
          ...(draft.config.gates_custom ? { gates_custom: draft.config.gates_custom } : {}),
        }
        break
      case 'prompts':
        sections.prompts = {
          prompt_selections: draft.prompt_selections ?? emptyPromptSelections(),
          prompt_overrides: draft.prompt_overrides,
        }
        break
      case 'text':
        sections.text = { title: draft.title, request: draft.request }
        break
      case 'context':
        sections.context = {
          work_items: draft.work_items,
          document_ids: draft.document_ids,
          context_summary_ids: draft.context_summary_ids,
        }
        break
    }
  }
  return sections
}

/**
 * Write sections into a draft. The merge goes through parseDraftPayload, so a
 * stored preset is normalized exactly like a stored draft (unknown fields drop,
 * malformed values fall back to defaults).
 */
export function applySections(draft: WizardDraft, sections: PresetSections): WizardDraft {
  const raw: Record<string, unknown> = { ...draft, config: { ...draft.config } }
  const config = raw['config'] as Record<string, unknown>
  const systems = sections.systems
  if (isRecord(systems) && isRecord(systems['scope'])) raw['scope'] = systems['scope']
  if (isRecord(sections.execution)) Object.assign(config, sections.execution)
  if (isRecord(sections.phases)) {
    delete config['gates_custom']
    Object.assign(config, sections.phases)
  }
  if (isRecord(sections.prompts)) {
    raw['prompt_selections'] = sections.prompts['prompt_selections']
    raw['prompt_overrides'] = sections.prompts['prompt_overrides'] ?? {}
    raw['prompt_override_removals'] = []
  }
  if (isRecord(sections.text)) Object.assign(raw, sections.text)
  if (isRecord(sections.context)) Object.assign(raw, sections.context)
  const next = parseDraftPayload(raw)
  return {
    ...next,
    launch_idempotency_key: draft.launch_idempotency_key,
    preset: draft.preset ?? null,
    // Keep the prompt UI's focus valid for the (possibly new) phase plan.
    config: { ...next.config, prompt_focus_phase: focusFor(next.config, draft.config.prompt_focus_phase) },
  }
}

function focusFor(config: WizardConfig, previous: PhaseName | null): PhaseName | null {
  const phases = selectedPhases(config)
  return previous && phases.includes(previous) ? previous : phases[0] ?? null
}

/** Organization bundle → the draft sections it sets (ported from the wizard's former picker). */
export function organizationSections(assistantId: string, bundle: Record<string, unknown>): PresetSections {
  const config: Record<string, unknown> = {
    engine: 'sim',
    unsupported_engine: null,
    golden_config_id: assistantId,
    golden_configurable: { ...bundle },
    analysis: normalizeAnalysis(bundle['analysis']),
  }
  const engine = bundle['engine']
  if (typeof engine === 'string' && (ENGINES as readonly string[]).includes(engine)) config['engine'] = engine as EngineId
  else if (engine !== undefined && engine !== null) config['unsupported_engine'] = String(engine)
  const ownership = bundle['phase_execution'] as { owners?: Record<string, string> } | undefined
  const phases = ownership?.owners
    ? normalizePhases(PHASE_NAMES.filter(p => ownership.owners?.[p] === 'langgraph'))
    : normalizePhases(selectedPhasesView(bundle))
  const gates = bundle['gates'] !== undefined && bundle['gates'] !== null
  const sections: PresetSections = {
    execution: config,
    phases: {
      phases,
      schema_version: 2,
      migrated_phase_ownership: !ownership && phases !== null,
      gates_mode: gates ? 'custom' : 'all_gated',
      ...(gates ? { gates_custom: normalizeGateMatrix(bundle['gates']) } : {}),
    },
    prompts: {
      prompt_selections: parsePromptSelections(bundle['prompt_selections']) ?? emptyPromptSelections(),
      prompt_overrides: {},
    },
  }
  const topology = parseTestScope(bundle['test_scope'])
  const primary = topology?.participants.find(p => p.id === topology.primary_participant_id)
  const traffic = topology?.participants.find(p => p.id === topology.traffic_entry_participant_id)
  const project = primary?.project_id ?? bundle['project_id']
  const app = primary?.application_id ?? bundle['app_id']
  if (topology || typeof app === 'string') {
    sections.systems = {
      scope: {
        test_scope: topology,
        project_id: typeof project === 'string' ? project : '',
        app_id: typeof app === 'string' ? app : null,
        environment_id: topology ? traffic?.environment_id ?? null : typeof bundle['environment_id'] === 'string' ? bundle['environment_id'] : null,
      },
    }
  }
  return sections
}

/** The primary application an organization bundle is for (metadata wins over the bundle). */
export function organizationApplication(bundle: Record<string, unknown>, metadata?: Record<string, unknown> | null): string | null {
  const explicit = metadata?.['application_id']
  if (typeof explicit === 'string' && explicit) return explicit
  const topology = parseTestScope(bundle['test_scope'])
  const primary = topology?.participants.find(p => p.id === topology.primary_participant_id)
  const app = primary?.application_id ?? bundle['app_id']
  return typeof app === 'string' && app ? app : null
}

export function primaryApplication(draft: WizardDraft): string | null {
  const topology = draft.scope.test_scope
  const primary = topology?.participants.find(p => p.id === topology.primary_participant_id)
  return primary?.application_id || draft.scope.app_id || null
}

/**
 * Apply a preset: write its sections and remember them as the baseline. When the
 * owning project or primary application changes, context tied to the old ones
 * (documents; tickets and summaries for a project change) is cleared, as before.
 */
export function applyPreset(draft: WizardDraft, preset: Pick<Preset, 'kind' | 'id' | 'version' | 'name' | 'sections'>): WizardDraft {
  const next = writeSections(draft, preset.sections)
  const link: DraftPresetLink = {
    kind: preset.kind,
    id: preset.id,
    version: preset.version,
    name: preset.name,
    baseline: sectionsFromDraft(next, Object.keys(preset.sections) as SectionId[]),
  }
  // An organization preset launches through its assistant; a team preset keeps
  // whatever assistant its saved execution section named.
  return { ...next, preset: link }
}

/** Write sections, dropping context that belonged to a project or application they moved away from. */
function writeSections(draft: WizardDraft, sections: PresetSections): WizardDraft {
  const next = applySections(draft, sections)
  const projectChanged = next.scope.project_id !== draft.scope.project_id
  const appChanged = next.scope.app_id !== draft.scope.app_id
  return {
    ...next,
    ...(projectChanged || appChanged ? { document_ids: sections.context ? next.document_ids : [] } : {}),
    ...(projectChanged && !sections.context ? { work_items: [], context_summary_ids: [] } : {}),
  }
}

/**
 * Start a test from a preset with the primary system chosen on Start. When the
 * preset is for that same application, the project chosen on Start keeps
 * owning the test (an application can belong to several projects).
 */
export function applyPresetFromStart(
  draft: WizardDraft,
  preset: Pick<Preset, 'kind' | 'id' | 'version' | 'name' | 'sections'>,
  primary: { projectId: string; applicationId: string } | null,
): WizardDraft {
  const applied = applyPreset(draft, preset)
  const topology = applied.scope.test_scope
  if (!primary || !topology || primaryApplication(applied) !== primary.applicationId || applied.scope.project_id === primary.projectId) return applied
  const kept: WizardDraft = {
    ...applied,
    scope: {
      ...applied.scope,
      project_id: primary.projectId,
      test_scope: {
        ...topology,
        participants: topology.participants.map(p => p.id === topology.primary_participant_id ? { ...p, project_id: primary.projectId } : p),
      },
    },
  }
  // The Start choice is not an edit of the preset.
  const baseline = { ...(applied.preset!.baseline as PresetSections), ...sectionsFromDraft(kept, ['systems']) }
  return { ...kept, preset: { ...applied.preset!, baseline } }
}

/** Put back every value the preset set (keeping the link). */
export function resetToPreset(draft: WizardDraft): WizardDraft {
  if (!draft.preset) return draft
  return { ...writeSections(draft, draft.preset.baseline as PresetSections), preset: draft.preset }
}

/** Keep the current values but stop tracking (and launching through) the preset. */
export function detachPreset(draft: WizardDraft): WizardDraft {
  return { ...draft, preset: null, config: { ...draft.config, golden_config_id: null } }
}

// ── Fields: labels, summaries, changes ──────────────────────────────────────

export interface FieldNames {
  application?: (id: string) => string | undefined
  environment?: (id: string) => string | undefined
}

export interface PresetField {
  id: string
  section: SectionId
  label: string
  value: (draft: WizardDraft) => unknown
  format: (draft: WizardDraft, names: FieldNames) => string
}

function stable(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`
  if (isRecord(value)) {
    return `{${Object.keys(value).sort().filter(key => value[key] !== undefined).map(key => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value ?? null)
}

const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`

function minutes(seconds: unknown): string {
  const value = Number(seconds)
  if (!Number.isFinite(value) || value <= 0) return '—'
  if (value % 3600 === 0) return `${value / 3600} h`
  if (value % 60 === 0) return `${value / 60} min`
  return `${value} s`
}

export function engineLabel(draft: WizardDraft): string {
  return draft.config.unsupported_engine ? `Unsupported engine (${draft.config.unsupported_engine})` : ENGINE_CAPTIONS[draft.config.engine].label
}

export function workloadLabel(draft: WizardDraft): string {
  const load = effectiveWorkload(draft.config.golden_configurable?.['load_test'])
  const vusers = load['vusers']
  const slas = load.slas
  const parts = [
    typeof vusers === 'number' ? `${vusers} VUsers` : null,
    `${minutes(load['duration_s'])} run`,
    typeof slas['p95_ms'] === 'number' ? `p95 ≤ ${slas['p95_ms']} ms` : null,
  ]
  return parts.filter(Boolean).join(' · ')
}

export function phasesLabel(draft: WizardDraft): string {
  const agent = selectedPhases(draft.config)
  if (agent.length === PHASE_NAMES.length) return 'All 7 phases by agent'
  if (agent.length === 0) return 'Every phase completed by a person'
  return `${agent.length} of 7 phases by agent (${agent.map(phase => PHASE_LABELS[phase]).join(', ')})`
}

export function reviewLabel(draft: WizardDraft): string {
  return draft.config.gates_mode === 'all_gated' ? 'Review every phase' : draft.config.gates_mode === 'all_auto' ? 'Run automatically' : 'Custom reviews'
}

export function systemsLabel(draft: WizardDraft, names: FieldNames = {}): string {
  const topology = draft.scope.test_scope
  const label = (app: string, env: string | null | undefined) => {
    const appName = names.application?.(app) ?? app
    const envName = env ? names.environment?.(env) ?? env : null
    return envName ? `${appName} (${envName})` : appName
  }
  if (topology?.participants.length) {
    const ordered = [...topology.participants].sort((a, b) => (a.id === topology.primary_participant_id ? -1 : b.id === topology.primary_participant_id ? 1 : 0))
    return ordered.filter(p => p.application_id).map(p => label(p.application_id, p.environment_id)).join(' · ') || 'Not chosen'
  }
  return draft.scope.app_id ? label(draft.scope.app_id, draft.scope.environment_id) : 'Not chosen'
}

export const PRESET_FIELDS: PresetField[] = [
  { id: 'systems', section: 'systems', label: 'Systems under test', value: d => d.scope, format: (d, n) => systemsLabel(d, n) },
  { id: 'engine', section: 'execution', label: 'Engine', value: d => [d.config.engine, d.config.unsupported_engine ?? null], format: d => engineLabel(d) },
  { id: 'workload', section: 'execution', label: 'Workload', value: d => d.config.golden_configurable?.['load_test'] ?? null, format: d => workloadLabel(d) },
  { id: 'analysis', section: 'execution', label: 'Analysis tools', value: d => d.config.analysis?.selections ?? [], format: d => (d.config.analysis?.selections ?? []).map(s => ANALYSIS_LABELS[s.tool]).join(', ') || 'None' },
  { id: 'phases', section: 'phases', label: 'Phase owners', value: d => d.config.phases, format: d => phasesLabel(d) },
  { id: 'reviews', section: 'phases', label: 'Review policy', value: d => [d.config.gates_mode, d.config.gates_mode === 'custom' ? d.config.gates_custom ?? null : null], format: d => reviewLabel(d) },
  { id: 'prompts', section: 'prompts', label: 'Prompts', value: d => [d.prompt_selections ?? null, d.prompt_overrides], format: d => promptsLabel(d) },
  { id: 'title', section: 'text', label: 'Title', value: d => d.title, format: d => d.title || 'Empty' },
  { id: 'request', section: 'text', label: 'Request', value: d => d.request, format: d => d.request ? truncate(d.request, 80) : 'Empty' },
  { id: 'tickets', section: 'context', label: 'Tickets', value: d => d.work_items, format: d => plural(d.work_items.length, 'ticket') },
  { id: 'knowledge', section: 'context', label: 'Knowledge', value: d => [d.document_ids, d.context_summary_ids], format: d => plural(d.document_ids.length + d.context_summary_ids.length, 'document') },
]

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

export function promptsLabel(draft: WizardDraft): string {
  const selections = draft.prompt_selections
  const pinned = selections ? [selections.system, ...Object.values(selections.phases ?? {})].filter(Boolean).length : 0
  const contexts = (selections?.application.length ?? 0) + (selections?.environment.length ?? 0)
  const overrides = Object.keys(draft.prompt_overrides).length
  const parts = [plural(pinned, 'prompt selection'), contexts ? plural(contexts, 'context prompt') : null, overrides ? plural(overrides, 'override') : null]
  return parts.filter(Boolean).join(' · ')
}

/** The fields a set of sections covers, in display order. */
export function fieldsFor(sections: PresetSections | readonly SectionId[]): PresetField[] {
  const ids = Array.isArray(sections) ? sections : Object.keys(sections)
  return PRESET_FIELDS.filter(field => ids.includes(field.section))
}

export interface FieldChange {
  field: PresetField
  before: string
  after: string
}

/** Fields (within `sections`) whose values differ between two drafts. */
export function fieldChanges(before: WizardDraft, after: WizardDraft, sections: readonly SectionId[], names: FieldNames = {}): FieldChange[] {
  return fieldsFor(sections)
    .filter(field => stable(field.value(before)) !== stable(field.value(after)))
    .map(field => ({ field, before: field.format(before, names), after: field.format(after, names) }))
}

/** What the person changed since applying the draft's preset. */
export function changesSincePreset(draft: WizardDraft, names: FieldNames = {}): FieldChange[] {
  if (!draft.preset) return []
  const baseline = applySections(draft, draft.preset.baseline as PresetSections)
  return fieldChanges(baseline, draft, Object.keys(draft.preset.baseline) as SectionId[], names)
}

/** A preset rendered as a draft (for summaries and previews). */
export function presetDraft(preset: Pick<Preset, 'sections'>): WizardDraft {
  return applySections(parseDraftPayload({}), preset.sections)
}

/** Compact one-line summary for lists: systems · engine · phases · reviews · workload. */
export function presetSummary(preset: Pick<Preset, 'sections'>, names: FieldNames = {}): string[] {
  const draft = presetDraft(preset)
  const parts: string[] = []
  if (preset.sections.systems) parts.push(systemsLabel(draft, names))
  if (preset.sections.execution) parts.push(engineLabel(draft), workloadLabel(draft))
  if (preset.sections.phases) parts.push(phasesLabel(draft), reviewLabel(draft))
  return parts
}

/** Phase label helper re-exported for pages that list phases. */
export function phaseName(phase: PhaseName): string {
  return PHASE_LABELS[phase]
}

/**
 * Normalization (e.g. the Prompts step filling catalog defaults) is not an
 * edit: when it touches a section the person hasn't changed since applying
 * the preset, move the preset's baseline along with it.
 */
export function rebaseNormalizedPreset(previous: WizardDraft, next: WizardDraft): WizardDraft {
  const link = next.preset
  if (!link) return next
  const touched = (Object.keys(link.baseline) as SectionId[]).filter(id => SECTION_IDS.includes(id))
  const unchanged = touched.filter(id =>
    stable(sectionsFromDraft(previous, [id])[id]) === stable((link.baseline as PresetSections)[id]) &&
    stable(sectionsFromDraft(next, [id])[id]) !== stable((link.baseline as PresetSections)[id]))
  if (unchanged.length === 0) return next
  return { ...next, preset: { ...link, baseline: { ...link.baseline, ...sectionsFromDraft(next, unchanged) } } }
}
