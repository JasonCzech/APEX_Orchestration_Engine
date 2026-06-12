/**
 * Pure-logic contract tests for wizardState: per-step validation, the
 * PHASE_PREREQUISITES mirror's warn-only hints, gates_mode -> explicit matrix
 * mapping (the shape the backend's PipelineConfigurable.gates expects), and
 * the exact launch payload builder.
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

describe('phase prerequisites (warn, never block)', () => {
  it('warns when a prereq is not earlier in the plan, in backend phrasing', () => {
    expect(phaseDependencyHints(['execution', 'reporting'])).toEqual([
      'execution needs script_scenario earlier in plan or succeeded on thread',
    ])
    expect(phaseDependencyHints(['reporting'])).toEqual([
      'reporting needs execution earlier in plan or succeeded on thread',
    ])
    // Full canonical plan satisfies everything; env_triage has no prereqs.
    expect(phaseDependencyHints([...PHASE_NAMES])).toEqual([])
    expect(phaseDependencyHints(['env_triage'])).toEqual([])
  })

  it('normalizePhases keeps canonical order and collapses all/none to null', () => {
    expect(normalizePhases(['reporting', 'execution'])).toEqual(['execution', 'reporting'])
    expect(normalizePhases([...PHASE_NAMES])).toBeNull()
    expect(normalizePhases([])).toBeNull()
    expect(normalizePhases(['bogus'])).toBeNull()
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
      metadata: { project_id: 'demo', title: 'Checkout soak' },
      input: { title: 'Checkout soak', request: 'Soak the checkout flow' },
      configurable: {
        project_id: 'demo',
        engine: 'sim',
        gates: allGatedMatrix(),
      },
    })
  })

  it('includes scope ids, phase subset, overrides and prefixed context refs', () => {
    const draft = validDraft()
    draft.scope.app_id = 'app-checkout'
    draft.scope.environment_id = 'env-staging'
    draft.config.engine = 'apex_load'
    draft.config.phases = ['execution', 'reporting']
    draft.config.gates_mode = 'all_auto'
    draft.work_item_keys = ['PHX-241']
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
      phases: ['execution', 'reporting'],
      gates: ALL_AUTO_GATES,
      prompt_overrides: { 'phase/execution': { content: 'Custom system prompt' } },
      pre_execution_context: ['workitem:PHX-241', 'document:doc-9'],
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
})
