import { describe, expect, it } from 'vitest'
import { executionResultLabel, normalizedPhases, phaseProgress, orchestrationStatusLabel } from './runDisplay'

describe('operator results and phase consistency', () => {
  it('requires recorded outcomes, including a failed SLA after successful orchestration', () => {
    expect(executionResultLabel({ execution_result: 'failed' })).toBe('Failed')
    expect(executionResultLabel({ execution_result: 'passed' })).toBe('Passed')
    expect(executionResultLabel({})).toBe('Not available')
    expect(executionResultLabel({ execution_result: null })).toBe('Not available')
    expect(orchestrationStatusLabel('interrupted')).toBe('Awaiting approval')
  })
  it('uses validated state over a stale summary and excludes unselected phases', () => {
    const strip = normalizedPhases({ phases_plan: ['execution', 'reporting'], phase_results: { execution: { status: 'failed' }, reporting: { status: 'pending' } } }, [{ phase: 'execution', status: 'succeeded' }, { phase: 'reporting', status: 'succeeded' }])
    expect(strip.find((entry) => entry.phase === 'execution')?.status).toBe('failed')
    expect(strip.find((entry) => entry.phase === 'story_analysis')?.status).toBe('none')
    expect(phaseProgress(strip)).toEqual({ completed: 1, total: 2 })
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
    expect(phaseProgress(normalizedPhases({}, [{ phase: 'execution', status: 'succeeded' }]))).toEqual({ completed: 0, total: 0 })
    const excluded = normalizedPhases({ phases_plan: ['reporting'], phase_results: { execution: { status: 'succeeded', attempt: 3 } } })
    expect(excluded.find((entry) => entry.phase === 'execution')).toEqual({ phase: 'execution', status: 'none', attempt: undefined })
  })
  it('marks a selected phase without results pending, and permits an explicit summary fallback', () => {
    expect(normalizedPhases({ phases_plan: ['execution'] }).find((entry) => entry.phase === 'execution')?.status).toBe('pending')
    expect(normalizedPhases({ phases_plan: ['execution'] }, [{ phase: 'execution', status: 'none' }], true).find((entry) => entry.phase === 'execution')?.status).toBe('pending')
    expect(phaseProgress(normalizedPhases({}, [{ phase: 'execution', status: 'aborted' }, { phase: 'reporting', status: 'skipped' }], true))).toEqual({ completed: 2, total: 2 })
  })
})
