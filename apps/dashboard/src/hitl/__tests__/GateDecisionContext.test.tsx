import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { PipelineState } from '@apex/pipeline-events'
import { GateDecisionContext } from '../GateDecisionContext'

const state = {
  phases_plan: ['execution', 'reporting', 'postmortem'],
  phase_results: { execution: { status: 'succeeded', test_summary: { engine: 'sim', passed: true, kpis: { p95_ms: 212 } }, acceptance_thresholds: { p95_ms: 500 } } },
} as unknown as PipelineState

describe('GateDecisionContext', () => {
  it.each(['execution', 'reporting', 'postmortem'])('shows execution results beside the %s output review', (phase) => {
    render(<GateDecisionContext state={state} phase={phase} kind="phase_review" />)
    expect(screen.getByRole('group', { name: 'Execution results' })).toBeInTheDocument()
  })

  it.each([['story_analysis', 'phase_review'], ['reporting', 'prompt_review']])('omits them for %s %s', (phase, kind) => {
    render(<GateDecisionContext state={state} phase={phase} kind={kind} />)
    expect(screen.queryByRole('group', { name: 'Execution results' })).not.toBeInTheDocument()
  })
})
