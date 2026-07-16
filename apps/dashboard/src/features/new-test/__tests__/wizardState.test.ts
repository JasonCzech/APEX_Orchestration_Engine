/**
 * Pure-logic contract tests for wizardState: per-step validation, the
 * PHASE_PREREQUISITES mirror's new-thread validation, gates_mode -> explicit matrix
 * mapping (the shape the backend's PipelineConfigurable.gates expects), and
 * the launch-plan builder.
 */
import { describe, expect, it } from 'vitest'

import { PHASE_NAMES } from '@apex/pipeline-events'

import { ALL_AUTO_GATES } from '@/features/runs/launchRun'

import {
  allGatedMatrix,
  allIssues,
  buildLaunchPreview,
  emptyDraft,
  gateMatrixOf,
  isStepValid,
  normalizePhases,
  parseDraftPayload,
  phaseDependencyHints,
  type WizardDraft,
} from '../wizardState'

function validDraft(): WizardDraft {
  const draft = emptyDraft()
  draft.title = 'Checkout soak'
  draft.request = 'Soak the checkout flow'
  return draft
}

describe('wizardState validation', () => {
  it('scope requires title, request and project; later steps are optional', () => {
    const draft = emptyDraft()
    expect(isStepValid(draft, 'scope')).toBe(false)
    expect(isStepValid(draft, 'work-items')).toBe(true)
    expect(isStepValid(draft, 'context')).toBe(true)
    expect(isStepValid(draft, 'config')).toBe(true)

    const filled = validDraft()
    expect(isStepValid(filled, 'scope')).toBe(true)

    filled.config.phases = []
    filled.prompt_overrides['phase/execution'] = { content: '   ' }
    const issues = allIssues(filled)
    expect(issues).toEqual([
      { step: 'config', message: 'Select at least one phase' },
      {
        step: 'prompts',
        message: 'Override for phase/execution is empty — edit it or revert to catalog',
      },
    ])
  })
})

describe('phase prerequisites', () => {
  it('reports when a prerequisite is absent from the new thread plan', () => {
    expect(phaseDependencyHints(['execution', 'reporting'])).toEqual([
      'execution requires script_scenario earlier in this new run',
    ])
    expect(phaseDependencyHints(['reporting'])).toEqual([
      'reporting requires execution earlier in this new run',
    ])
    // Full canonical plan satisfies everything; env_triage has no prereqs.
    expect(phaseDependencyHints([...PHASE_NAMES])).toEqual([])
    expect(phaseDependencyHints(['env_triage'])).toEqual([])
  })

  it('normalizePhases keeps canonical order and preserves an explicit empty plan', () => {
    expect(normalizePhases(['reporting', 'execution'])).toEqual(['execution', 'reporting'])
    expect(normalizePhases([...PHASE_NAMES])).toBeNull()
    expect(normalizePhases([])).toEqual([])
    expect(normalizePhases(['bogus'])).toEqual([])
  })
})

describe('gates mapping to the configurable matrix', () => {
  it('maps all three modes onto explicit 7-phase matrices', () => {
    const config = emptyDraft().config

    const gated = gateMatrixOf({ ...config, gates_mode: 'all_gated' })
    expect(Object.keys(gated)).toEqual([...PHASE_NAMES])
    for (const policy of Object.values(gated)) {
      expect(policy).toEqual({ prompt_review: 'gated', output_review: 'gated' })
    }

    // all_auto reuses D2's exported matrix rather than re-deriving it.
    expect(gateMatrixOf({ ...config, gates_mode: 'all_auto' })).toBe(ALL_AUTO_GATES)

    const custom = allGatedMatrix()
    custom.execution = { prompt_review: 'auto', output_review: 'gated' }
    const matrix = gateMatrixOf({ ...config, gates_mode: 'custom', gates_custom: custom })
    expect(matrix.execution).toEqual({ prompt_review: 'auto', output_review: 'gated' })
    expect(matrix.story_analysis).toEqual({ prompt_review: 'gated', output_review: 'gated' })
  })
})

describe('buildLaunchPreview', () => {
  it('builds the exact payload: omits empty optionals, includes explicit gates', () => {
    const draft = validDraft()
    expect(buildLaunchPreview(draft)).toEqual({
      assistant_id: 'pipeline',
      metadata: { project_id: 'demo', title: 'Checkout soak' },
      input: { title: 'Checkout soak', request: 'Soak the checkout flow' },
      configurable: {
        project_id: 'demo',
        engine: 'sim',
        gates: allGatedMatrix(),
      },
      document_ids: [],
      work_item_keys: [],
    })
  })

  it('includes scope ids, phase subset, overrides and context selections', () => {
    const draft = validDraft()
    draft.scope.app_id = 'app-checkout'
    draft.scope.environment_id = 'env-staging'
    draft.config.engine = 'apex_load'
    draft.config.phases = ['execution', 'reporting']
    draft.config.gates_mode = 'all_auto'
    draft.work_items = [
      { key: 'PHX-241', connection_id: 'conn-jira', provider: 'jira' },
    ]
    draft.document_ids = ['doc-9']
    draft.prompt_overrides['phase/execution'] = { content: 'Custom system prompt' }

    const preview = buildLaunchPreview(draft)
    expect(preview.metadata).toEqual({
      project_id: 'demo',
      app_id: 'app-checkout',
      title: 'Checkout soak',
    })
    expect(preview.configurable).toEqual({
      project_id: 'demo',
      app_id: 'app-checkout',
      environment_id: 'env-staging',
      engine: 'apex_load',
      connections: { work_tracking: 'conn-jira' },
      phases: ['execution', 'reporting'],
      gates: ALL_AUTO_GATES,
      prompt_overrides: { 'phase/execution': { content: 'Custom system prompt' } },
    })
    expect(preview.document_ids).toEqual(['doc-9'])
    expect(preview.work_item_keys).toEqual(['PHX-241'])
  })

  it('requires legacy selections to be rebound and rejects mixed connections', () => {
    const draft = validDraft()
    draft.work_items = [
      { key: 'PHX-241', connection_id: null, provider: null },
    ]
    expect(allIssues(draft)).toContainEqual({
      step: 'work-items',
      message: 'Revalidate legacy work items before launch',
    })

    draft.work_items = [
      { key: 'PHX-241', connection_id: 'conn-jira', provider: 'jira' },
      { key: 'ADO-8', connection_id: 'conn-ado', provider: 'ado' },
    ]
    expect(allIssues(draft)).toContainEqual({
      step: 'work-items',
      message: 'Selected work items must use one work-tracking connection',
    })
  })

  it('retains unedited golden fields while visible controls override the bundle', () => {
    const draft = validDraft()
    draft.config.golden_config_id = 'asst-gold'
    draft.config.golden_configurable = {
      engine: 'loadrunner',
      connections: { execution: 'conn-7' },
      agent_backend: 'anthropic',
      model_by_phase: { reporting: 'claude-sonnet' },
      limits: { max_revise_loops: 6, poll_interval_s: 10 },
      prompt_overrides: { 'phase/reporting': { version_id: 'ver-9' } },
    }
    draft.config.engine = 'apex_load'
    draft.prompt_overrides['phase/execution'] = { content: 'Run-specific prompt' }

    const preview = buildLaunchPreview(draft)
    expect(preview.assistant_id).toBe('asst-gold')
    expect(preview.configurable).toMatchObject({
      engine: 'apex_load',
      connections: { execution: 'conn-7' },
      agent_backend: 'anthropic',
      model_by_phase: { reporting: 'claude-sonnet' },
      limits: { max_revise_loops: 6, poll_interval_s: 10 },
      prompt_overrides: {
        'phase/reporting': { version_id: 'ver-9' },
        'phase/execution': { content: 'Run-specific prompt' },
      },
    })
  })
})

describe('parseDraftPayload', () => {
  it('round-trips a full draft and survives junk payloads field-by-field', () => {
    const draft = validDraft()
    draft.config.phases = ['execution', 'reporting']
    draft.config.gates_mode = 'custom'
    draft.config.gates_custom = allGatedMatrix()
    draft.prompt_overrides['phase/execution'] = { content: 'x' }
    expect(parseDraftPayload(JSON.parse(JSON.stringify(draft)))).toEqual(draft)

    const junk = parseDraftPayload({
      title: 42,
      scope: { project_id: 'p1', app_id: 7 },
      config: { engine: 'warp-drive', phases: 'all', gates_mode: 'sometimes' },
      prompt_overrides: { 'phase/execution': { content: 9 }, ok: { content: 'kept' } },
    })
    expect(junk.title).toBe('')
    expect(junk.scope).toEqual({ project_id: 'p1', app_id: null, environment_id: null })
    expect(junk.config.engine).toBe('sim')
    expect(junk.config.phases).toBeNull()
    expect(junk.config.gates_mode).toBe('all_gated')
    expect(junk.prompt_overrides).toEqual({ ok: { content: 'kept' } })
  })

  it('restores legacy bare keys as explicitly unbound work-item references', () => {
    expect(parseDraftPayload({ work_item_keys: ['PHX-241', 'PHX-241'] }).work_items).toEqual([
      { key: 'PHX-241', connection_id: null, provider: null },
    ])
  })

  it('prefers an exact binding over a duplicate legacy reference for the same key', () => {
    expect(
      parseDraftPayload({
        work_items: [
          { key: 'PHX-241', connection_id: null, provider: null },
          { key: 'PHX-241', connection_id: 'conn-jira', provider: 'jira' },
        ],
      }).work_items,
    ).toEqual([
      { key: 'PHX-241', connection_id: 'conn-jira', provider: 'jira' },
    ])
  })
})
