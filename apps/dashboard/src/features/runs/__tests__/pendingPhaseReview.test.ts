import { describe, expect, it } from 'vitest'
import type { PipelineState } from '@apex/pipeline-events'
import type { GateInstance } from '@/hitl/gateMachine'
import { pendingPhaseReview } from '../pendingPhaseReview'
import { deriveTimeline } from '../timelineModel'

const gate: GateInstance = {
  interrupt_id: 'output-2', kind: 'phase_review', phase: 'execution',
  payload: { schema_version: 1, kind: 'phase_review', phase: 'execution', summary: 'Engine collected 10 VUsers.',
    result_preview: { reasoning_digest: 'Reviewed the collected evidence.' },
    evidence: { attempt: 2, provenance: 'simulator' },
    artifacts: [{ id: 'results', name: 'results.json', kind: 'engine_results' }],
    warnings: [], dialogue_tail: [], actions: ['approve', 'revise', 'abort'] },
}

describe('pending phase review projection', () => {
  it('projects current output and artifact references without inventing acceptance or timing', () => {
    const state: PipelineState = { phase_results: { execution: { status: 'pending' } } }
    const view = pendingPhaseReview('execution', state, gate)
    expect(view.entry).toMatchObject({ status: 'awaiting_output_review', attempt: 2, summary: 'Engine collected 10 VUsers.', artifact_ids: ['results'], provenance: 'simulator' })
    expect(view.artifacts).toEqual([{ id: 'results', name: 'results.json', kind: 'engine_results' }])
    expect(view.entry?.started_at).toBeUndefined()
    expect(view.entry?.ended_at).toBeUndefined()
    expect(view.entry?.duration_s).toBeUndefined()
    expect(state).toEqual({ phase_results: { execution: { status: 'pending' } } })
  })

  it('does not borrow old attempt summaries, prompts, KPIs or timing', () => {
    const state: PipelineState = { phase_results: { execution: { status: 'succeeded', attempt: 1,
      started_at: '2026-01-01T00:00:00Z', duration_s: 10, summary: 'Old output', resolved_prompt: { system: 'Old prompt', user: 'Old request' },
      test_summary: { passed: true }, approvals: [{ id: 'review-old', actor: 'consumer-id' }] } } }
    const view = pendingPhaseReview('execution', state, gate)
    expect(view.entry?.summary).toBe('Engine collected 10 VUsers.')
    expect(view.entry?.resolved_prompt).toBeUndefined()
    expect(view.entry?.test_summary).toBeUndefined()
    expect(view.entry?.started_at).toBeUndefined()
    expect(view.entry?.approvals).toEqual(state.phase_results?.execution?.approvals)
    expect(pendingPhaseReview('reporting', state, gate).pending).toBeNull()
  })

  it('keeps same-attempt recorded start and accepts previews without optional evidence', () => {
    const state: PipelineState = { phase_results: { execution: { attempt: 2, started_at: '2026-01-01T00:00:00Z' } } }
    expect(pendingPhaseReview('execution', state, gate).entry?.started_at).toBe('2026-01-01T00:00:00Z')
    const withoutEvidence = { ...gate, payload: gate.payload?.kind === 'phase_review' ? { ...gate.payload, evidence: undefined } : null }
    expect(pendingPhaseReview('execution', {}, withoutEvidence).entry?.summary).toBe('Engine collected 10 VUsers.')
  })

  it('orders recorded events by actual time across timestamp offsets', () => {
    const events = deriveTimeline({ phase_results: {
      story_analysis: { started_at: '2026-10-18T10:00:00+02:00' },
      test_planning: { started_at: '2026-10-18T09:00:00Z' },
    } })
    expect(events.map(event => event.label)).toEqual(['Story Analysis started', 'Test Planning started'])
  })

  it('adds a current review to the timeline without fabricated lifecycle or decision events', () => {
    const events = deriveTimeline({}, gate)
    expect(events).toEqual([expect.objectContaining({ at: null, label: 'Execution output awaiting review', detail: expect.stringContaining('output has not been accepted') })])
    expect(events[0]?.detail).toContain('1 artifact reference')
    expect(deriveTimeline({}, null)).toEqual([])
  })
})
