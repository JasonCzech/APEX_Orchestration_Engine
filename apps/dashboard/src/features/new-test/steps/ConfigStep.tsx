import { ReleaseMappingPicker } from '../ReleaseMappingPicker'
import { AnalysisFields } from './AnalysisFields'
import { WorkloadFields } from './WorkloadFields'
import { RadioChoices } from '@/components/ux/RadioChoices'
import { PHASE_LABELS } from '@/features/runs/runDisplay'
import { reviewPolicySummary } from '../reviewPolicySummary'
/**
 * Step 2 — Config: engine radio cards (presets are chosen on Start or from the
 * wizard's preset bar), phase-subset toggle strip with blocking dependency errors (mirrors
 * PHASE_PREREQUISITES for the new thread), and the gates segmented control with the 7x2 custom
 * matrix (checked = gated).
 */

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'


import type { StepProps } from '../NewRunWizard'
import {
  allGatedMatrix,
  ENGINES,
  focusedPromptPhase,
  selectedPhases,
  withPhaseOwner,
  ENGINE_CAPTIONS,
  type GatesMode,
  type WizardConfig,
} from '../wizardState'

const GATES_MODES: { id: GatesMode; label: string; caption: string }[] = [
  { id: 'all_gated', label: 'Review every phase', caption: 'Every phase pauses for both reviews' },
  { id: 'all_auto', label: 'Run automatically', caption: 'Agent phases run without review pauses; phases a person completes still wait for them' },
  { id: 'custom', label: 'Custom', caption: 'Pick per phase and per gate' },
]

function phaseLabel(phase: PhaseName): string {
  return PHASE_LABELS[phase]
}

export function ConfigStep({ draft, onChange }: StepProps) {
  const config = draft.config
  const phases = selectedPhases(config)
  const promptFocus = focusedPromptPhase(config)
  const hints: string[] = []

  function patchConfig(patch: Partial<WizardConfig>) {
    onChange((prev) => ({ ...prev, config: { ...prev.config, ...patch } }))
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

  return (
    <section className="wizard-step" aria-label="Config">
      {config.unsupported_engine && <p className="tonal-card danger" role="alert">The selected preset pins engine “{config.unsupported_engine}”, which this deployment does not support. Choose a supported engine before launch.</p>}
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
