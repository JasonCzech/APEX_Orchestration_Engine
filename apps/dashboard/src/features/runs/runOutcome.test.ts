import { describe, expect, it } from 'vitest'
import { executionResultLabel, executionWasOmitted, orchestrationStatusLabel } from './runDisplay'
import type { PipelineSummary } from '@/api/hooks/usePipelines'
const strip = (status: string) => [{ phase: 'story_analysis', status }, { phase: 'execution', status: 'none' }] as PipelineSummary['phase_strip']
describe('outcome meaning', () => {
  it('separates completed analysis from test acceptance', () => {
    expect(orchestrationStatusLabel('idle', strip('succeeded'))).toBe('Orchestration complete')
    expect(executionResultLabel({ phase_strip: strip('succeeded') })).toBe('Not available')
    expect(executionResultLabel({})).toBe('Not available')
  })
  it('does not turn stopped or failed plans into completion', () => {
    expect(orchestrationStatusLabel('idle', strip('failed'))).toBe('Orchestration failed')
    expect(orchestrationStatusLabel('idle', strip('aborted'))).toBe('Stopped by operator')
    expect(orchestrationStatusLabel('idle', strip('pending'))).toBe('Orchestration stopped')
    expect(executionResultLabel({ execution_result: 'failed', phase_strip: strip('succeeded') })).toBe('Not available')
    expect(executionResultLabel({ execution_result: 'failed', phase_strip: [{ phase: 'execution', status: 'succeeded' }] })).toBe('Failed')
  })
})


describe('execution evidence provenance', () => {
  it('keeps omitted execution distinct from an established test verdict', () => {
    const omitted = { phases_plan: ['story_analysis'] } as import('@apex/pipeline-events').PipelineState
    expect(executionWasOmitted(omitted)).toBe(true)
    expect(executionResultLabel({}, omitted)).toBe('Not available')
    expect(executionWasOmitted({})).toBe(false)
    expect(executionResultLabel({ execution_result: 'failed' }, omitted)).toBe('Not available')
    expect(executionResultLabel({}, { phases_plan: ['execution'] })).toBe('Not available')
  })
  it('keeps imported or retained execution evidence applicable to analysis-only plans', () => {
    const plan = { phases_plan: ['reporting'] } as import('@apex/pipeline-events').PipelineState
    for (const evidence of [
      { phase_results: { execution: { status: 'succeeded', test_summary: { passed: null } } } },
      { external_results: { uri: 'https://load.example/run/123' } },
      { engine_handle: { engine: 'apex_load', external_run_id: '123' } },
    ]) {
      const state = { ...plan, ...evidence } as import('@apex/pipeline-events').PipelineState
      expect(executionResultLabel({ phase_strip: strip('succeeded') }, state)).toBe('Not available')
    }
    expect(executionResultLabel({ phase_strip: strip('succeeded') }, {})).toBe('Not available')
  })
})
