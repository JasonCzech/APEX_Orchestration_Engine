import { describe, expect, it } from 'vitest'
import { executionResultLabel, formatDuration, normalizedPhases, withLiveRunningPhase, phaseProgress, phaseProgressLabel, pipelineStatusLabel, orchestrationStatusLabel } from './runDisplay'

describe('operator results and phase consistency', () => {
  it('requires recorded outcomes, including a failed SLA after successful orchestration', () => {
    expect(executionResultLabel({ execution_result: 'failed', phase_strip: [{ phase: 'execution', status: 'succeeded' }] })).toBe('Failed')
    expect(executionResultLabel({ execution_result: 'passed', phase_strip: [{ phase: 'execution', status: 'succeeded' }] })).toBe('Passed')
    expect(executionResultLabel({})).toBe('Not available')
    expect(executionResultLabel({ execution_result: null })).toBe('Not available')
    expect(orchestrationStatusLabel('interrupted')).toBe('Awaiting review')
    expect(orchestrationStatusLabel('interrupted', [{ phase: 'reporting', status: 'awaiting_human_completion' }])).toBe('Awaiting human completion')
  })
  it('uses validated state over a stale summary and excludes unselected phases', () => {
    const strip = normalizedPhases({ phases_plan: ['execution', 'reporting'], phase_results: { execution: { status: 'failed' }, reporting: { status: 'pending' } } }, [{ phase: 'execution', status: 'succeeded' }, { phase: 'reporting', status: 'succeeded' }])
    expect(strip.find((entry) => entry.phase === 'execution')?.status).toBe('failed')
    expect(strip.find((entry) => entry.phase === 'story_analysis')?.status).toBe('none')
    expect(phaseProgress(strip)).toMatchObject({ completed: 1, total: 2 })
  })
  it('retains independently validated phases when unrelated state fields drift', () => {
    const strip = normalizedPhases({ phase_results: { execution: { status: 'failed', attempt: 2 } } },
      [{ phase: 'execution', status: 'succeeded', attempt: 1 }, { phase: 'reporting', status: 'succeeded', attempt: 1 }], true)
    expect(strip.find((entry) => entry.phase === 'execution')).toEqual({ phase: 'execution', status: 'failed', attempt: 2 })
    expect(strip.find((entry) => entry.phase === 'reporting')?.status).toBe('succeeded')
    const driftedPlan = { phases_plan: ['future_phase'], phase_results: { execution: { status: 'failed' } } } as unknown as import('@apex/pipeline-events').PipelineState
    expect(normalizedPhases(driftedPlan, [], true).find((entry) => entry.phase === 'execution')?.status).toBe('failed')
  })
  it('does not silently mix a valid empty snapshot with a stale summary', () => {
    expect(phaseProgress(normalizedPhases({}, [{ phase: 'execution', status: 'succeeded' }]))).toMatchObject({ completed: 0, total: 0 })
    const excluded = normalizedPhases({ phases_plan: ['reporting'], phase_results: { execution: { status: 'succeeded', attempt: 3 } } })
    expect(excluded.find((entry) => entry.phase === 'execution')).toEqual({ phase: 'execution', status: 'none', attempt: undefined })
  })
  it('marks a selected phase without results pending, and permits an explicit summary fallback', () => {
    expect(normalizedPhases({ phases_plan: ['execution'] }).find((entry) => entry.phase === 'execution')?.status).toBe('pending')
    expect(normalizedPhases({ phases_plan: ['execution'] }, [{ phase: 'execution', status: 'none' }], true).find((entry) => entry.phase === 'execution')?.status).toBe('pending')
    expect(phaseProgress(normalizedPhases({}, [{ phase: 'execution', status: 'aborted' }, { phase: 'reporting', status: 'skipped' }], true))).toMatchObject({ completed: 2, total: 2 })
  })
})

describe('honest lifecycle projections', () => {
  it.each([
    [{ thread_status: 'interrupted', pending_gate: { interrupt_id: 'human-task', kind: 'human_completion', phase: 'execution' } }, 'awaiting_human_completion'],
    [{ thread_status: 'error', current_phase: 'execution' }, 'failed'],
  ] as const)('retains phase ownership and provenance when projecting %j', (context, status) => {
    const state = {
      phases_plan: ['execution' as const],
      run_config: { phase_execution: { owners: { execution: 'hitl' } } },
      phase_results: { execution: { status: 'pending' as const, attempt: 2, completed_by: 'operator-1' } },
    }
    expect(normalizedPhases(state, [], false, context).find(entry => entry.phase === 'execution')).toEqual({
      phase: 'execution', status, attempt: 2, executor: 'hitl', completed_by: 'operator-1',
    })
    const recordedOwner = { ...state, phase_results: { execution: { ...state.phase_results.execution, executor: 'langgraph' } } }
    expect(normalizedPhases(recordedOwner, [], false, context).find(entry => entry.phase === 'execution')?.executor).toBe('langgraph')
  })
  it('uses the current gate and terminal failure without inventing a successful phase', () => {
    const state = { phases_plan: ['execution' as const], phase_results: { execution: { status: 'pending' as const } } }
    const failed = normalizedPhases(state, [], false, { thread_status: 'error', current_phase: 'execution' })
    expect(failed.find(entry => entry.phase === 'execution')?.status).toBe('failed')
    const gated = normalizedPhases(state, [], false, { thread_status: 'interrupted', pending_gate: { interrupt_id: 'gate', kind: 'phase_review', phase: 'execution' } })
    expect(gated.find(entry => entry.phase === 'execution')?.status).toBe('awaiting_output_review')
    expect(phaseProgressLabel(phaseProgress(failed))).toBe('0/1 phases succeeded · 1 failed')
  })
  it('distinguishes missing plans and unknown states from pending work', () => {
    expect(pipelineStatusLabel('future_state')).toBe('Unknown')
    expect(phaseProgressLabel(phaseProgress(normalizedPhases({})))).toBe('Phase plan not recorded for this run')
  })
  it.each(['running', 'awaiting_output_review', 'failed', 'aborted', 'pending'])('withholds result for %s execution', (status) => {
    expect(executionResultLabel({ execution_result: 'failed', phase_strip: [{ phase: 'execution', status }] })).toBe('Not available')
  })
  it('uses one missing-result label even when execution is omitted', () => {
    expect(executionResultLabel({}, { phases_plan: ['reporting'] })).toBe('Not available')
  })
})

describe('live running display over a parent checkpoint', () => {
  const pending = () => normalizedPhases({ phases_plan: ['execution', 'reporting'], phase_results: { execution: { status: 'pending', attempt: 1 }, reporting: { status: 'pending', attempt: 1 } } })
  const context = { thread_status: 'busy', current_phase: 'script_scenario', pending_gate: null }
  const live = { runId: 'active-run', stream: { status: 'live', phaseProgress: { execution: { status: 'running', attempt: 1 } } } }

  it('projects only the observed running phase and keeps the source snapshot unchanged', () => {
    const strip = pending()
    expect(withLiveRunningPhase(strip, context, live).find(item => item.phase === 'execution')?.status).toBe('running')
    expect(withLiveRunningPhase(strip, context, live).find(item => item.phase === 'reporting')?.status).toBe('pending')
    expect(strip.find(item => item.phase === 'execution')?.status).toBe('pending')
  })
  it.each(['idle', 'error', 'interrupted', undefined])('keeps canonical phases when thread is %s', thread_status => {
    const strip = pending()
    expect(withLiveRunningPhase(strip, { ...context, thread_status }, live)).toBe(strip)
  })
  it.each(['idle', 'connecting', 'ended', 'error'])('rejects live overlays from %s transport', status => {
    const strip = pending()
    expect(withLiveRunningPhase(strip, context, { ...live, stream: { ...live.stream, status } })).toBe(strip)
  })
  it.each([undefined, 0, 2])('requires an exact recorded attempt, rejecting %s', attempt => {
    expect(withLiveRunningPhase(pending(), context, { ...live, stream: { ...live.stream, phaseProgress: { execution: { status: 'running', attempt } } } }).find(item => item.phase === 'execution')?.status).toBe('pending')
  })
  it.each(['succeeded', 'failed', 'aborted', 'skipped', 'awaiting_prompt_review', 'awaiting_output_review', 'unknown', 'none'])('preserves canonical %s', status => {
    const strip = pending().map(item => item.phase === 'execution' ? { ...item, status } : item)
    expect(withLiveRunningPhase(strip, context, live).find(item => item.phase === 'execution')?.status).toBe(status)
  })
  it('does not resurrect an old phase, cross a gate, or combine ambiguous live runs', () => {
    const strip = pending()
    expect(withLiveRunningPhase(strip, { ...context, current_phase: 'reporting' }, live)).toBe(strip)
    expect(withLiveRunningPhase(strip, { ...context, pending_gate: { interrupt_id: 'gate', phase: 'reporting', kind: 'prompt_review' } }, live)).toBe(strip)
    expect(withLiveRunningPhase(strip, context, { ...live, runId: null })).toBe(strip)
    expect(withLiveRunningPhase(strip, context, { ...live, stream: { ...live.stream, phaseProgress: { ...live.stream.phaseProgress, reporting: { status: 'running', attempt: 1 } } } })).toBe(strip)
    expect(withLiveRunningPhase(strip, context, { ...live, stream: { ...live.stream, phaseProgress: { ...live.stream.phaseProgress, reporting: { status: 'awaiting_output_review', attempt: 1 } } } })).toBe(strip)
    const gated = strip.map(item => item.phase === 'reporting' ? { ...item, status: 'awaiting_prompt_review' } : item)
    expect(withLiveRunningPhase(gated, context, live)).toBe(gated)
  })
})


it('requires recorded successful execution before publishing any verdict', () => {
  expect(executionResultLabel({ execution_result: 'failed' })).toBe('Not available')
  expect(executionResultLabel({ execution_result: 'passed' })).toBe('Not available')
  expect(executionResultLabel({}, { phase_results: { execution: { test_summary: { engine: 'stub', passed: false, kpis: {}, sla_breaches: [] } } } })).toBe('Not available')
})

it.each([[59.99, '1m'], [119.9, '2m'], [3599.9, '1h'], [7199.9, '2h'], [0.9999, '1s'], [65, '1m 5s'], [Infinity, '—'], [NaN, '—'], [-1, '—']] as const)('formats duration %s without overflowed units', (seconds, label) => {
  expect(formatDuration(seconds)).toBe(label)
})
