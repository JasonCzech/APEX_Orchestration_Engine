import type { components } from '@apex/api-client'
import { PHASE_NAMES, type PhaseName, type PipelineState } from '@apex/pipeline-events'
import { beforeEach, describe, expect, it } from 'vitest'

import { buildConfigurable, emptyDraft } from '@/features/new-test/wizardState'

import { createDevDataStore } from './store'

type PromptSummary = components['schemas']['PromptSummary']

async function prepareLaunch(bypassed: readonly PhaseName[]) {
  const store = createDevDataStore()
  const response = await store.handleApexRequest(new Request('http://localhost/v1/prompts'))
  const catalog = await response!.json() as PromptSummary[]
  const select = (namespace: string, phase?: PhaseName) => {
    const prompt = catalog.find(row => row.namespace === namespace && row.is_default && row.selection_enabled && (!phase || row.phase === phase))!
    return { prompt_id: prompt.id, version_id: prompt.active_version!.id }
  }
  const draft = emptyDraft()
  draft.title = 'Launch with phase ownership'
  draft.scope.project_id = 'acc-perf'
  draft.scope.test_scope = {
    schema_version: 1,
    participants: [
      { id: 'acc', project_id: 'acc-perf', application_id: 'app-acc-perf-acc', environment_id: 'env-acc-perf-acc-perfhyper2', layer: 'frontend' },
      { id: 'bsse', project_id: 'acc-perf', application_id: 'app-acc-perf-bsse', environment_id: 'env-acc-perf-bsse-perf-bsse', layer: 'backend' },
    ],
    primary_participant_id: 'acc',
    traffic_entry_participant_id: 'acc',
  }
  draft.config.phases = PHASE_NAMES.filter(phase => !bypassed.includes(phase))
  // Retain saved selections as the UI does when an agent phase is bypassed.
  // The actual wizard builder must exclude them from the launch request.
  draft.prompt_selections = {
    schema_version: 1,
    system: select('system'),
    phases: Object.fromEntries(PHASE_NAMES.map(phase => [phase, select('phase', phase)])),
    application: [],
    environment: [],
  }
  const configurable = buildConfigurable(draft)
  const launch = () => store.handleApexRequest(new Request('http://localhost/v1/pipelines', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ title: draft.title, project_id: draft.scope.project_id, idempotency_key: 'ownership-launch', configurable }),
  }))
  return { store, configurable, launch }
}

describe('demo launch prompt ownership', () => {
  beforeEach(() => localStorage.clear())

  it.each([
    { name: 'all agents', bypassed: [] },
    ...PHASE_NAMES.map(phase => ({ name: `bypass ${phase}`, bypassed: [phase] })),
    { name: 'multiple bypassed phases', bypassed: ['env_triage', 'execution'] as PhaseName[] },
    { name: 'all phases completed by people', bypassed: [...PHASE_NAMES] },
  ])('accepts the real wizard payload for $name', async ({ bypassed }) => {
    const { store, configurable, launch } = await prepareLaunch(bypassed)
    const response = await launch()
    expect(response?.status, await response?.clone().text()).toBe(202)
    const result = await response!.json()
    expect(await (await launch())!.json()).toEqual(result)
    const detail = await (await store.handleApexRequest(new Request(`http://localhost/v1/pipelines/${result.thread_id}`)))!.json()
    const state = detail.values as PipelineState
    const config = state.run_config as Record<string, unknown>
    expect(config.phases).toEqual(PHASE_NAMES)
    expect(config.phase_execution).toEqual(configurable.phase_execution)
    const manifest = config.prompt_manifest as { phases: Record<string, unknown> } | undefined
    const agentPhases = PHASE_NAMES.filter(phase => !bypassed.includes(phase))
    if (agentPhases.length) {
      expect(Object.keys(manifest!.phases)).toEqual(agentPhases)
      expect(Object.keys(state.prompt_reviews ?? {})).toEqual(agentPhases)
    } else {
      expect(manifest).toBeUndefined()
    }
  })

  it('keeps legacy subset launches without ownership working', async () => {
    const { configurable, launch } = await prepareLaunch(PHASE_NAMES.filter(phase => phase !== 'reporting'))
    delete configurable.phase_execution
    configurable.phases = ['reporting']
    expect((await launch())?.status).toBe(202)
  })

  it.each(['missing agent prompt', 'invalid version', 'prompt for bypassed phase'])('still rejects %s', async problem => {
    const { configurable, launch } = await prepareLaunch(['env_triage'])
    const selections = configurable.prompt_selections as {
      phases: Record<string, { prompt_id: string; version_id: string }>
    }
    if (problem === 'missing agent prompt') delete selections.phases.reporting
    if (problem === 'invalid version') selections.phases.reporting!.version_id = 'missing-version'
    if (problem === 'prompt for bypassed phase') selections.phases.env_triage = selections.phases.reporting!
    expect((await launch())?.status).toBe(422)
  })
})
