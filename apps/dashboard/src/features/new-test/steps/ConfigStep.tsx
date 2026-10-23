import { parseTestScope } from '../testScope'
import { parsePromptSelections, emptyPromptSelections } from '@/features/prompts/promptCategories'
import { ReleaseMappingPicker } from '../ReleaseMappingPicker'
import { AnalysisFields } from './AnalysisFields'
import { WorkloadFields } from './WorkloadFields'
import { RadioChoices } from '@/components/ux/RadioChoices'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { reviewPolicySummary } from '../reviewPolicySummary'
/**
 * Step 2 — Config: engine radio cards, golden-config picker (assistants),
 * phase-subset toggle strip with blocking dependency errors (mirrors
 * PHASE_PREREQUISITES for the new thread), and the gates segmented control with the 7x2 custom
 * matrix (checked = gated).
 */
import { Dialog } from '@/components/Dialog'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { usePromptList } from '@/api/hooks/usePrompts'
import { useAssistants, type GoldenConfig } from '@/api/hooks/useAssistants'
import { CachedDataWarning } from '@/components/CachedDataWarning'
import { selectedPhasesView } from '@/features/golden-configs/configView'

import type { StepProps } from '../NewRunWizard'
import {
  allGatedMatrix,
  ENGINES,
  focusedPromptPhase,
  normalizeGateMatrix,
  normalizePhases,
  normalizeAnalysis,
  selectedPhases,
  withPhaseOwner,
  type EngineId,
  type GatesMode,
  type WizardConfig,
} from '../wizardState'

const ENGINE_CAPTIONS: Record<EngineId, { label: string; caption: string }> = {
  sim: {
    label: 'Simulated',
    caption: 'Built-in simulator — no external system, instant feedback. Default.',
  },
  apex_load: {
    label: 'APEX Load',
    caption: 'Distributed load generation via the APEX Load engine.',
  },
  loadrunner: {
    label: 'LoadRunner',
    caption: 'OpenText LoadRunner enterprise suite integration.',
  },
}

const GATES_MODES: { id: GatesMode; label: string; caption: string }[] = [
  { id: 'all_gated', label: 'Review every phase', caption: 'Every phase pauses for both reviews' },
  { id: 'all_auto', label: 'Run automatically', caption: 'Agent phases run without review pauses; phases a person completes still wait for them' },
  { id: 'custom', label: 'Custom', caption: 'Pick per phase and per gate' },
]

function phaseLabel(phase: PhaseName): string {
  return PHASE_LABELS[phase]
}

export function ConfigStep({ draft, onChange }: StepProps) {
  const [pendingPreset, setPendingPreset] = useState<GoldenConfig | null>(null)
  const assistants = useAssistants()
  const promptCatalog = usePromptList({ selection_enabled: true })
  const config = draft.config
  const phases = selectedPhases(config)
  const promptFocus = focusedPromptPhase(config)
  const hints: string[] = []

  function patchConfig(patch: Partial<WizardConfig>) {
    onChange((prev) => ({ ...prev, config: { ...prev.config, ...patch } }))
  }

  function applyGoldenConfig(golden: GoldenConfig) {
    const selected = draft.prompt_selections
    const requiredSelections = [selected?.system, ...Object.values(selected?.phases ?? {})]
    const hasPromptChanges = Boolean(selected?.application.length || selected?.environment.length) ||
      requiredSelections.some(item => item && (item.override_content !== undefined ||
        !promptCatalog.data?.some(row => row.is_default && row.id === item.prompt_id && row.active_version?.id === item.version_id)))
    if (hasPromptChanges || Object.keys(draft.prompt_overrides).length > 0 || draft.document_ids.length > 0 ||
      draft.work_items.length > 0 || draft.scope.environment_id || draft.config.golden_config_id ||
      draft.config.engine !== 'sim' || draft.config.unsupported_engine || draft.config.phases !== null ||
      draft.config.gates_mode !== 'all_gated' || draft.config.golden_configurable?.load_test) {
      setPendingPreset(golden)
    } else commitGoldenConfig(golden)
  }

  function commitGoldenConfig(golden: GoldenConfig) {
    onChange((prev) => {
      const bundle = golden.configurable
      const next: WizardConfig = {
        ...prev.config,
        // A newly selected assistant inherits PipelineConfigurable defaults,
        // never UI overrides left behind by the previously selected assistant.
        engine: 'sim',
        unsupported_engine: null,
        gates_mode: 'all_gated',
        golden_config_id: golden.assistantId,
        golden_configurable: { ...bundle },
      }
      delete next.gates_custom
      const engine = bundle['engine']
      if (typeof engine === 'string' && (ENGINES as readonly string[]).includes(engine)) {
        next.engine = engine as EngineId
      } else if (engine !== undefined && engine !== null) {
        next.unsupported_engine = String(engine)
      }
      if (bundle['gates'] !== undefined && bundle['gates'] !== null) {
        next.gates_mode = 'custom'
        next.gates_custom = normalizeGateMatrix(bundle['gates'])
      }
      const ownership = bundle['phase_execution'] as { owners?: Record<string, string> } | undefined
      next.phases = ownership?.owners ? normalizePhases(PHASE_NAMES.filter(p => ownership.owners?.[p] === 'langgraph')) : normalizePhases(selectedPhasesView(bundle))
      next.analysis = normalizeAnalysis(bundle['analysis'])
      next.migrated_phase_ownership = !ownership && next.phases !== null
      next.schema_version = 2
      const nextPhases = selectedPhases(next)
      if (!next.prompt_focus_phase || !nextPhases.includes(next.prompt_focus_phase)) {
        next.prompt_focus_phase = nextPhases[0] ?? null
      }
      const topology = parseTestScope(bundle['test_scope'])
      const primary = topology?.participants.find(p => p.id === topology.primary_participant_id)
      const traffic = topology?.participants.find(p => p.id === topology.traffic_entry_participant_id)
      const project = primary?.project_id ?? bundle['project_id']
      const app = primary?.application_id ?? bundle['app_id']
      const environment = topology ? traffic?.environment_id ?? null : bundle['environment_id']
      const projectId = typeof project === 'string' && project.length > 0 ? project : prev.scope.project_id
      const projectChanged = projectId !== prev.scope.project_id
      const appId =
        typeof app === 'string' && app.length > 0
          ? app
          : projectChanged
            ? null
            : prev.scope.app_id
      const appChanged = appId !== prev.scope.app_id
      const environmentId = topology ? traffic?.environment_id ?? null :
        typeof environment === 'string' && environment.length > 0
          ? environment
          : projectChanged || appChanged
            ? null
            : prev.scope.environment_id
      return {
        ...prev,
        scope: {
          test_scope: topology ?? (!projectChanged && !appChanged && environmentId === prev.scope.environment_id ? prev.scope.test_scope : undefined),
          project_id: projectId,
          app_id: appId,
          environment_id: environmentId,
        },
        config: next,
        prompt_selections: parsePromptSelections(bundle['prompt_selections']) ?? emptyPromptSelections(),
        prompt_selection_issues: ['Validating prompt selections…'],
        prompt_overrides: {},
        prompt_override_removals: [],
        ...(projectChanged || appChanged
          ? {
              document_ids: [],
              work_items: projectChanged ? [] : prev.work_items,
              context_summary_ids: projectChanged ? [] : prev.context_summary_ids,
            }
          : {}),
      }
    })
  }

  function clearGoldenConfig() {
    onChange((prev) => ({
      ...prev,
      config: { ...prev.config, golden_config_id: null, golden_configurable: null },
      prompt_override_removals: [],
    }))
  }

  function togglePhase(phase: PhaseName) {
    onChange((prev) => ({ ...prev, config: withPhaseOwner(prev.config, phase, !selectedPhases(prev.config).includes(phase)) }))
  }

  function setGatesMode(mode: GatesMode) {
    patchConfig({
      gates_mode: mode,
      // Entering custom seeds the matrix (all gated) so the checkboxes have state.
      ...(mode === 'custom' && !config.gates_custom ? { gates_custom: allGatedMatrix() } : {}),
    })
  }

  function toggleGate(phase: PhaseName, gate: 'prompt_review' | 'output_review') {
    const matrix = config.gates_custom ?? allGatedMatrix()
    const entry = matrix[phase]
    patchConfig({
      gates_custom: {
        ...matrix,
        [phase]: { ...entry, [gate]: entry[gate] === 'gated' ? 'auto' : 'gated' },
      },
    })
  }

  const goldenConfigs = assistants.data ?? []

  // D7 (additive): /golden-configs detail deep-link — ?golden=<assistant_id>
  // preselects the matching golden config once the picker data arrives, then
  // strips the param so Clear sticks. Track the actual param value so an
  // in-place back/forward navigation to another golden config is not ignored.
  const [searchParams, setSearchParams] = useSearchParams()
  const goldenParam = searchParams.get('golden')
  const goldenAppliedRef = useRef<string | null>(null)
  useEffect(() => {
    if (!goldenParam) {
      goldenAppliedRef.current = null
      return
    }
    if (goldenAppliedRef.current === goldenParam) return
    const match = goldenConfigs.find((golden) => golden.assistantId === goldenParam)
    if (!match) return
    goldenAppliedRef.current = goldenParam
    applyGoldenConfig(match)
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        next.delete('golden')
        return next
      },
      { replace: true },
    )
  })

  return (
    <section className="wizard-step" aria-label="Config">
      {pendingPreset && <Dialog ariaLabel="Apply test preset" className="glass-panel wizard-confirm" overlayClassName="wizard-confirm-overlay" onClose={() => setPendingPreset(null)}>
        <h2>Apply {pendingPreset.name}?</h2>
        <p>This replaces the engine, phase selection, review policy, workload, prompt selections and all prompt overrides. If its project or application differs, attached documents are removed; a different project also removes work items and context summaries.</p>
        <div className="wizard-row"><button type="button" className="btn btn-secondary" onClick={() => setPendingPreset(null)}>Cancel</button><button type="button" className="btn btn-primary" onClick={() => { commitGoldenConfig(pendingPreset); setPendingPreset(null) }}>Apply preset</button></div>
      </Dialog>}
      {config.unsupported_engine && <p className="tonal-card danger" role="alert">This preset pins engine “{config.unsupported_engine}”, which this deployment does not support. Choose a supported engine before launch.</p>}
      <div className="wizard-field">
        <span className="wizard-label">Engine</span>
        <div className="wizard-engine-cards" role="radiogroup" aria-label="Execution engine">
          {ENGINES.map((engine) => (
            <button
              key={engine}
              type="button"
              role="radio"
              aria-checked={!config.unsupported_engine && config.engine === engine}
              tabIndex={config.engine === engine ? 0 : -1}
              onKeyDown={(event) => {
                const index = ENGINES.indexOf(engine)
                const next = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? (index + 1) % ENGINES.length
                  : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? (index - 1 + ENGINES.length) % ENGINES.length
                    : event.key === 'Home' ? 0 : event.key === 'End' ? ENGINES.length - 1 : -1
                if (next < 0) return
                event.preventDefault()
                patchConfig({ engine: ENGINES[next]!, unsupported_engine: null })
                event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="radio"]')[next]?.focus()
              }}
              className={`glass-panel wizard-engine-card${
                !config.unsupported_engine && config.engine === engine ? ' wizard-engine-card--selected' : ''
              }`}
              onClick={() => patchConfig({ engine, unsupported_engine: null })}
            >
              <span className="wizard-engine-name">{ENGINE_CAPTIONS[engine].label}</span>
              <span className="wizard-caption">{ENGINE_CAPTIONS[engine].caption}</span>
            </button>
          ))}
        </div>
      </div>

      {config.engine === 'apex_load' && <ReleaseMappingPicker draft={draft} onChange={onChange} />}
      <div className="wizard-field">
        <span className="wizard-label">Test preset</span>
        {config.golden_config_id && (
          <div className="wizard-row">
            <span className="topbar-meta-chip accent" data-testid="config-inherited-chip">
              config inherited
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={clearGoldenConfig}
            >
              Clear
            </button>
          </div>
        )}
        {assistants.isError && assistants.data && (
          <CachedDataWarning error={assistants.error} onRetry={() => void assistants.refetch()} />
        )}
        {assistants.isError && !assistants.data ? (
          <p className="wizard-caption wizard-caption--danger">Test presets failed to load. You can continue with the engine and review policy below; no new preset will be applied. <button type="button" className="btn btn-secondary btn-sm" onClick={() => void assistants.refetch()}>Retry test presets</button></p>
        ) : goldenConfigs.length === 0 ? (
          <p className="wizard-caption">
            {assistants.isLoading ? 'Loading…' : 'No test presets published yet.'}
          </p>
        ) : (
          <div className="wizard-golden-cards">
            {goldenConfigs.map((golden) => (
              <button
                key={golden.assistantId}
                type="button"
                className={`glass-panel wizard-golden-card${
                  config.golden_config_id === golden.assistantId
                    ? ' wizard-golden-card--selected'
                    : ''
                }`}
                aria-pressed={config.golden_config_id === golden.assistantId}
                onClick={() => applyGoldenConfig(golden)}
              >
                <span className="wizard-engine-name">{golden.name}</span>
                {golden.description && <span className="wizard-caption">{golden.description}</span>}
              </button>
            ))}
          </div>
        )}
      </div>

      <WorkloadFields draft={draft} onChange={onChange} />
      <div className="wizard-field">
        <span className="wizard-label">Phase execution</span>
        <p id="wizard-phase-execution-hint" className="wizard-caption">All phases are completed in order. Pressed phases are run by an agent; the others wait for a person to complete them.</p>
        <div className="wizard-phase-strip" role="group" aria-label="Phase execution">
          {PHASE_NAMES.map((phase) => (
            <button
              key={phase}
              type="button"
              className={`wizard-phase-toggle${
                phases.includes(phase) ? ' wizard-phase-toggle--on' : ''
              }${promptFocus === phase ? ' wizard-phase-toggle--focused' : ''}`}
              aria-label={phaseLabel(phase)}
              aria-describedby="wizard-phase-execution-hint"
              aria-pressed={phases.includes(phase)}
              aria-current={promptFocus === phase ? 'step' : undefined}
              onClick={() => togglePhase(phase)}
            >
              {phaseLabel(phase)}<span className="wizard-caption" aria-hidden="true">{phases.includes(phase) ? 'Agent' : 'Person'}</span>
            </button>
          ))}
        </div>
        {config.migrated_phase_ownership && <p className="tonal-card">This saved setup now assigns previously deselected phases to a person. Each will pause until they complete it.</p>}
        {hints.length > 0 && (
          <ul className="wizard-hint-list" data-testid="phase-dependency-hints">
            {hints.map((hint) => (
              <li key={hint} className="wizard-caption wizard-caption--danger">
                {hint}
              </li>
            ))}
          </ul>
        )}
      </div>

      <AnalysisFields draft={draft} onChange={onChange} />
      <div className="wizard-field">
        <span className="wizard-label">Review policy</span>
        <RadioChoices label="Review policy" value={config.gates_mode} options={GATES_MODES} onChange={setGatesMode} />
        <p className="wizard-caption">{reviewPolicySummary(config)}</p>
        {config.gates_mode === 'custom' && (
          <div className="data-table-wrap">
            <table className="data-table" data-testid="gates-matrix">
              <thead>
                <tr>
                  <th>Phase</th>
                  <th>Prompt review</th>
                  <th>Output review</th>
                </tr>
              </thead>
              <tbody>
                {PHASE_NAMES.map((phase) => {
                  const entry = (config.gates_custom ?? allGatedMatrix())[phase]
                  return (
                    <tr key={phase}>
                      <td>{phaseLabel(phase)}</td>
                      <td>
                        {!phases.includes(phase) ? <span>Human completion required</span> : <input
                          type="checkbox"
                          aria-label={`${phaseLabel(phase)} prompt review gated`}
                          checked={entry.prompt_review === 'gated'}
                          onChange={() => toggleGate(phase, 'prompt_review')}
                        />}
                      </td>
                      <td>
                        {!phases.includes(phase) ? <span>Human completion required</span> : <input
                          type="checkbox"
                          aria-label={`${phaseLabel(phase)} output review gated`}
                          checked={entry.output_review === 'gated'}
                          onChange={() => toggleGate(phase, 'output_review')}
                        />}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  )
}
