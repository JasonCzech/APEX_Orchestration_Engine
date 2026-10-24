import { describe, expect, it } from 'vitest'

import { emptyDraft, withPrimarySystem, type WizardDraft } from '@/features/new-test/wizardState'

import { reachableProjects } from '@/features/new-test/testScope'

import {
  applyPreset,
  applyPresetFromStart,
  changesSincePreset,
  detachPreset,
  fieldChanges,
  organizationApplication,
  organizationSections,
  parsePresetKey,
  presetSummary,
  resetToPreset,
  sectionsFromDraft,
  type Preset,
} from './presetModel'

const PRIMARY = { projectId: 'proj-a', applicationId: 'app-acc', layer: 'frontend' as const }

function teamPreset(sections: Preset['sections'], overrides: Partial<Preset> = {}): Preset {
  return {
    key: 'team:p1', kind: 'team', id: 'p1', name: 'ACC soak', description: null, applicationId: 'app-acc',
    projectId: 'proj-a', visibility: 'project', version: 3, updatedAt: null, updatedBy: null, owner: null,
    mine: false, editable: true, archived: false, sections, ...overrides,
  }
}

function configured(): WizardDraft {
  const draft = withPrimarySystem(emptyDraft(), PRIMARY)
  return {
    ...draft,
    title: 'Peak check',
    config: { ...draft.config, engine: 'apex_load', gates_mode: 'all_auto', golden_configurable: { load_test: { vusers: 200, duration_s: 1800 } } },
  }
}

describe('preset sections', () => {
  it('round-trips the sections it stores and leaves the rest of the draft alone', () => {
    const source = configured()
    const sections = sectionsFromDraft(source, ['execution', 'phases'])
    expect(Object.keys(sections)).toEqual(['execution', 'phases'])
    const target = { ...withPrimarySystem(emptyDraft(), { ...PRIMARY, applicationId: 'app-mule' }), title: 'Other test' }
    const applied = applyPreset(target, teamPreset(sections))
    expect(applied.config.engine).toBe('apex_load')
    expect(applied.config.gates_mode).toBe('all_auto')
    expect(applied.config.golden_configurable).toEqual({ load_test: { vusers: 200, duration_s: 1800 } })
    // Sections the preset doesn't include stay as they were.
    expect(applied.title).toBe('Other test')
    expect(applied.scope.app_id).toBe('app-mule')
    expect(applied.launch_idempotency_key).toBe(target.launch_idempotency_key)
    expect(applied.preset).toMatchObject({ kind: 'team', id: 'p1', version: 3, name: 'ACC soak' })
  })

  it('counts changes since the preset, resets them, and detaches without losing values', () => {
    const applied = applyPreset(withPrimarySystem(emptyDraft(), PRIMARY), teamPreset(sectionsFromDraft(configured(), ['execution', 'phases'])))
    expect(changesSincePreset(applied)).toEqual([])
    const edited: WizardDraft = { ...applied, config: { ...applied.config, gates_mode: 'all_gated', engine: 'sim' } }
    expect(changesSincePreset(edited).map(change => change.field.label)).toEqual(['Engine', 'Review policy'])
    // Title isn't part of the preset, so editing it is not a change from it.
    expect(changesSincePreset({ ...edited, title: 'Renamed' })).toHaveLength(2)
    const reset = resetToPreset(edited)
    expect(changesSincePreset(reset)).toEqual([])
    expect(reset.config.engine).toBe('apex_load')
    const detached = detachPreset(edited)
    expect(detached.preset).toBeNull()
    expect(detached.config.engine).toBe('sim')
  })

  it('clears documents, tickets and summaries when a preset moves the test to another project', () => {
    const draft: WizardDraft = { ...withPrimarySystem(emptyDraft(), PRIMARY), document_ids: ['doc-1'], work_items: [{ key: 'PERF-1', connection_id: null, provider: null }], context_summary_ids: ['sum-1'] }
    const elsewhere = sectionsFromDraft(withPrimarySystem(emptyDraft(), { projectId: 'proj-b', applicationId: 'app-opus', layer: 'backend' }), ['systems'])
    const applied = applyPreset(draft, teamPreset(elsewhere))
    expect(applied.scope.project_id).toBe('proj-b')
    expect([applied.document_ids, applied.work_items, applied.context_summary_ids]).toEqual([[], [], []])
  })

  it('clears the other project\'s context when Reset moves the test back to the preset\'s project', () => {
    const applied = applyPreset(withPrimarySystem(emptyDraft(), PRIMARY), teamPreset(sectionsFromDraft(withPrimarySystem(emptyDraft(), PRIMARY), ['systems'])))
    const moved = { ...withPrimarySystem(applied, { projectId: 'proj-b', applicationId: 'app-opus', layer: 'backend' }), preset: applied.preset, work_items: [{ key: 'B-1', connection_id: null, provider: null }], document_ids: ['doc-b'] }
    const reset = resetToPreset(moved)
    expect(reset.scope.project_id).toBe('proj-a')
    expect([reset.work_items, reset.document_ids]).toEqual([[], []])
  })

  it('keeps prompt choices and systems out of the execution section', () => {
    const draft = configured()
    draft.config.golden_configurable = { load_test: { vusers: 5 }, prompt_overrides: { 'phase/execution': { content: 'x' } }, test_scope: {}, project_id: 'proj-a' }
    expect(sectionsFromDraft(draft, ['execution']).execution?.golden_configurable).toEqual({ load_test: { vusers: 5 } })
    expect(sectionsFromDraft(draft, ['execution', 'prompts']).execution?.golden_configurable).toEqual({ load_test: { vusers: 5 }, prompt_overrides: { 'phase/execution': { content: 'x' } } })
  })

  it('keeps the project chosen on Start when the preset is for the same application', () => {
    const saved = sectionsFromDraft(withPrimarySystem(emptyDraft(), { ...PRIMARY, projectId: 'proj-b' }), ['systems', 'phases'])
    const start = withPrimarySystem(emptyDraft(), PRIMARY)
    const applied = applyPresetFromStart(start, teamPreset(saved, { projectId: 'proj-b' }), PRIMARY)
    expect(applied.scope.project_id).toBe('proj-a')
    expect(applied.scope.test_scope?.participants[0]?.project_id).toBe('proj-a')
    // Not counted as an edit of the preset.
    expect(changesSincePreset(applied)).toEqual([])
    // A preset for another application keeps its own systems.
    const other = sectionsFromDraft(withPrimarySystem(emptyDraft(), { projectId: 'proj-b', applicationId: 'app-opus', layer: 'backend' }), ['systems'])
    expect(applyPresetFromStart(start, teamPreset(other), PRIMARY).scope.project_id).toBe('proj-b')
  })

  it('describes the before → after of applying a preset', () => {
    const draft = withPrimarySystem(emptyDraft(), PRIMARY)
    const after = applyPreset(draft, teamPreset(sectionsFromDraft(configured(), ['execution'])))
    const changes = fieldChanges(draft, after, ['execution'])
    expect(changes.map(change => [change.field.label, change.before, change.after])).toEqual([
      ['Engine', 'Simulated', 'APEX Load'],
      ['Workload', '10 VUsers · 2 s run · p95 ≤ 500 ms', '200 VUsers · 30 min run · p95 ≤ 500 ms'],
    ])
  })
})

describe('organization presets', () => {
  const bundle = {
    engine: 'apex_load',
    app_id: 'app-acc',
    project_id: 'proj-a',
    environment_id: 'env-2',
    gates: { story_analysis: { prompt_review: 'auto', output_review: 'auto' } },
  }

  it('converts an assistant bundle into wizard sections, including its systems', () => {
    const sections = organizationSections('asst-1', bundle)
    expect(Object.keys(sections).sort()).toEqual(['execution', 'phases', 'prompts', 'systems'])
    const applied = applyPreset(emptyDraft(), { kind: 'organization', id: 'asst-1', version: 4, name: 'Release', sections })
    expect(applied.config.golden_config_id).toBe('asst-1')
    expect(applied.config.engine).toBe('apex_load')
    expect(applied.config.gates_mode).toBe('custom')
    expect(applied.scope).toMatchObject({ project_id: 'proj-a', app_id: 'app-acc', environment_id: 'env-2' })
  })

  it('flags engines this deployment does not know instead of guessing', () => {
    const sections = organizationSections('asst-2', { engine: 'apexload' })
    const applied = applyPreset(emptyDraft(), { kind: 'organization', id: 'asst-2', version: 1, name: 'Legacy', sections })
    expect(applied.config.unsupported_engine).toBe('apexload')
    expect(presetSummary({ sections })).toContain('Unsupported engine (apexload)')
  })

  it('maps a preset to its primary application, metadata first', () => {
    expect(organizationApplication(bundle)).toBe('app-acc')
    expect(organizationApplication(bundle, { application_id: 'app-other' })).toBe('app-other')
    expect(organizationApplication({})).toBeNull()
  })
})

describe('reachable projects', () => {
  const app = (id: string, project_ids: string[]) => ({ id, name: id, project_ids, description: null, archived_at: null, created_at: '', updated_at: '' })
  const apps = [app('app-a', ['proj-a', 'proj-b']), app('app-c', ['proj-c'])]
  it('offers only projects a scoped consumer can own a test in', () => {
    expect(reachableProjects(apps, null).sort()).toEqual(['proj-a', 'proj-b', 'proj-c'])
    expect(reachableProjects(apps, { scopes: [{ project_id: 'proj-b', app_id: null }] })).toEqual(['proj-b'])
    expect(reachableProjects(apps, { scopes: [{ project_id: 'proj-c', app_id: 'app-a' }] })).toEqual(['proj-c'])
  })
})

describe('preset keys', () => {
  it('parses kind:id and rejects anything else', () => {
    expect(parsePresetKey('team:abc')).toEqual({ kind: 'team', id: 'abc' })
    expect(parsePresetKey('organization:asst:v2')).toEqual({ kind: 'organization', id: 'asst:v2' })
    expect(parsePresetKey('bogus:1')).toBeNull()
    expect(parsePresetKey(null)).toBeNull()
  })
})
