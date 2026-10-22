import { selectedPhases, allGatedMatrix, type WizardConfig } from './wizardState'
export function reviewPolicySummary(config: WizardConfig): string {
  const human = 7 - selectedPhases(config).length
  const completion = ` ${human} human completion ${human === 1 ? 'step' : 'steps'}.`
  if (config.gates_mode === 'all_auto') return 'Automatic: no planned review pauses.' + completion
  const phases = selectedPhases(config)
  const matrix = config.gates_custom ?? allGatedMatrix()
  const prompt = phases.filter(phase => config.gates_mode === 'all_gated' || matrix[phase].prompt_review === 'gated').length
  const output = phases.filter(phase => config.gates_mode === 'all_gated' || matrix[phase].output_review === 'gated').length
  const total = prompt + output
  return `${total} planned review ${total === 1 ? 'pause' : 'pauses'}: ${prompt} prompt and ${output} output ${total === 1 ? 'review' : 'reviews'}. Revisions may require additional reviews.` + completion
}
