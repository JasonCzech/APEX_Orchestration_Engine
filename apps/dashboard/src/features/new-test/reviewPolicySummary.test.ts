import { describe, expect, it } from 'vitest'
import { emptyDraft, allGatedMatrix } from './wizardState'
import { reviewPolicySummary } from './reviewPolicySummary'
describe('planned review counts', () => {
  it('counts only selected phases and honors custom review choices', () => {
    const config = { ...emptyDraft().config, phases: ['story_analysis'] as const }
    expect(reviewPolicySummary({ ...config, phases: [...config.phases] })).toContain('2 planned review pauses: 1 prompt and 1 output')
    const matrix = allGatedMatrix()
    matrix.story_analysis.prompt_review = 'auto'
    expect(reviewPolicySummary({ ...config, phases: [...config.phases], gates_mode: 'custom', gates_custom: matrix })).toContain('1 planned review pause: 0 prompt and 1 output')
    expect(reviewPolicySummary({ ...config, phases: [...config.phases], gates_mode: 'all_auto' })).toContain('no planned review pauses')
  })
})
