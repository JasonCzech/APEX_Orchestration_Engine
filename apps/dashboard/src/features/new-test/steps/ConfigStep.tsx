/**
 * Step 4 — Config: engine radio cards, golden-config picker (assistants),
 * phase-subset toggle strip with warn-only dependency hints (mirrors
 * PHASE_PREREQUISITES), and the gates segmented control with the 7x2 custom
 * matrix (checked = gated).
 */
import { useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router'

import { PHASE_NAMES, type PhaseName } from '@apex/pipeline-events'

import { useAssistants, type GoldenConfig } from '@/api/hooks/useAssistants'

import type { StepProps } from '../NewRunWizard'
import {
  allGatedMatrix,
  ENGINES,
  normalizeGateMatrix,
  normalizePhases,
  phaseDependencyHints,
  selectedPhases,
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
  { id: 'all_gated', label: 'All gated', caption: 'Every phase pauses for both reviews' },
  { id: 'all_auto', label: 'All auto', caption: 'No pauses — phases run straight through' },
  { id: 'custom', label: 'Custom', caption: 'Pick per phase and per gate' },
]

function phaseLabel(phase: PhaseName): string {
  return phase.replaceAll('_', ' ')
}

export function ConfigStep({ draft, onChange }: StepProps) {
  const assistants = useAssistants()
  const config = draft.config
  const phases = selectedPhases(config)
  const hints = phaseDependencyHints(phases)

  function patchConfig(patch: Partial<WizardConfig>) {
    onChange((prev) => ({ ...prev, config: { ...prev.config, ...patch } }))
  }

  function applyGoldenConfig(golden: GoldenConfig) {
    onChange((prev) => {
      const bundle = golden.configurable
      const next: WizardConfig = { ...prev.config, golden_config_id: golden.assistantId }
      const engine = bundle['engine']
      if (typeof engine === 'string' && (ENGINES as readonly string[]).includes(engine)) {
        next.engine = engine as EngineId
      }
      if (bundle['gates'] !== undefined && bundle['gates'] !== null) {
        next.gates_mode = 'custom'
        next.gates_custom = normalizeGateMatrix(bundle['gates'])
      }
      if (Array.isArray(bundle['phases'])) {
        next.phases = normalizePhases(bundle['phases'])
      }
      return { ...prev, config: next }
    })
  }

  function togglePhase(phase: PhaseName) {
    const current = new Set(phases)
    if (current.has(phase)) current.delete(phase)
    else current.add(phase)
    const next = PHASE_NAMES.filter((name) => current.has(name))
    patchConfig({ phases: next.length === PHASE_NAMES.length ? null : next })
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
  // strips the param so Clear sticks. One-shot; a no-op without the param.
  const [searchParams, setSearchParams] = useSearchParams()
  const goldenParam = searchParams.get('golden')
  const goldenAppliedRef = useRef(false)
  useEffect(() => {
    if (goldenAppliedRef.current || !goldenParam) return
    const match = goldenConfigs.find((golden) => golden.assistantId === goldenParam)
    if (!match) return
    goldenAppliedRef.current = true
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
      <div className="wizard-field">
        <span className="wizard-label">Engine</span>
        <div className="wizard-engine-cards" role="radiogroup" aria-label="Execution engine">
          {ENGINES.map((engine) => (
            <button
              key={engine}
              type="button"
              role="radio"
              aria-checked={config.engine === engine}
              className={`glass-panel wizard-engine-card${
                config.engine === engine ? ' wizard-engine-card--selected' : ''
              }`}
              onClick={() => patchConfig({ engine })}
            >
              <span className="wizard-engine-name">{ENGINE_CAPTIONS[engine].label}</span>
              <span className="wizard-caption">{ENGINE_CAPTIONS[engine].caption}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="wizard-field">
        <span className="wizard-label">Golden config</span>
        {config.golden_config_id && (
          <div className="wizard-row">
            <span className="topbar-meta-chip accent" data-testid="config-inherited-chip">
              config inherited
            </span>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={() => patchConfig({ golden_config_id: null })}
            >
              Clear
            </button>
          </div>
        )}
        {assistants.isError ? (
          <p className="wizard-caption wizard-caption--danger">Golden configs failed to load</p>
        ) : goldenConfigs.length === 0 ? (
          <p className="wizard-caption">
            {assistants.isLoading ? 'Loading…' : 'No golden configs published yet.'}
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

      <div className="wizard-field">
        <span className="wizard-label">Phases</span>
        <div className="wizard-phase-strip" role="group" aria-label="Phase subset">
          {PHASE_NAMES.map((phase) => (
            <button
              key={phase}
              type="button"
              className={`wizard-phase-toggle${
                phases.includes(phase) ? ' wizard-phase-toggle--on' : ''
              }`}
              aria-pressed={phases.includes(phase)}
              onClick={() => togglePhase(phase)}
            >
              {phaseLabel(phase)}
            </button>
          ))}
        </div>
        {config.phases !== null && config.phases.length === 0 && (
          <p className="wizard-caption wizard-caption--danger">Select at least one phase</p>
        )}
        {hints.length > 0 && (
          <ul className="wizard-hint-list" data-testid="phase-dependency-hints">
            {hints.map((hint) => (
              <li key={hint} className="wizard-caption wizard-caption--warning">
                {hint}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="wizard-field">
        <label className="wizard-toggle-row">
          <span>
            <span className="wizard-label">Manual step-through</span>
            <span className="wizard-caption">
              Pause on every phase for prompt review and result approval.
            </span>
          </span>
          <input
            type="checkbox"
            checked={config.gates_mode === 'all_gated'}
            onChange={(event) => setGatesMode(event.target.checked ? 'all_gated' : 'all_auto')}
          />
        </label>
      </div>

      <div className="wizard-field">
        <span className="wizard-label">Gates</span>
        <div className="wizard-segmented" role="group" aria-label="Gates mode">
          {GATES_MODES.map((mode) => (
            <button
              key={mode.id}
              type="button"
              className={`wizard-segment${config.gates_mode === mode.id ? ' wizard-segment--on' : ''}`}
              aria-pressed={config.gates_mode === mode.id}
              title={mode.caption}
              onClick={() => setGatesMode(mode.id)}
            >
              {mode.label}
            </button>
          ))}
        </div>
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
                        <input
                          type="checkbox"
                          aria-label={`${phaseLabel(phase)} prompt review gated`}
                          checked={entry.prompt_review === 'gated'}
                          onChange={() => toggleGate(phase, 'prompt_review')}
                        />
                      </td>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={`${phaseLabel(phase)} output review gated`}
                          checked={entry.output_review === 'gated'}
                          onChange={() => toggleGate(phase, 'output_review')}
                        />
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
