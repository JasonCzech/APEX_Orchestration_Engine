import { selectedPhases, allGatedMatrix, type WizardConfig } from './wizardState'
export function reviewPolicySummary(config: WizardConfig): string {
  if (config.gates_mode === 'all_auto') return 'Automatic: no planned review pauses.'
  const phases = selectedPhases(config)
  const matrix = config.gates_custom ?? allGatedMatrix()
  const prompt = phases.filter(phase => config.gates_mode === 'all_gated' || matrix[phase].prompt_review === 'gated').length
  const output = phases.filter(phase => config.gates_mode === 'all_gated' || matrix[phase].output_review === 'gated').length
  return `${prompt + output} planned review pauses: ${prompt} prompt and ${output} output reviews. Revisions may require additional reviews.`
}
