import { describe, expect, it } from 'vitest'
import type { TestResultSummary } from '@apex/pipeline-events'
import { COMPARE_KPIS, kpiDifference, bestWorst, completedSummary, formatLimit, thresholdCell, thresholdsDiffer, unexplainedSimFailure } from './compareModel'

describe('accessible KPI differences', () => {
  const latency = COMPARE_KPIS.find((metric) => metric.key === 'p95_ms')!
  it('states direction, absolute and relative differences', () => {
    expect(kpiDifference(110, 100, latency)).toBe('+10 ms (10.0%) · Worse')
    expect(kpiDifference(90, 100, latency)).toBe('−10 ms (10.0%) · Better')
    expect(kpiDifference(0.007, 0.006, COMPARE_KPIS.find((metric) => metric.key === 'error_rate')!)).toBe('+0.10 percentage points (16.7%) · Worse')
  })
  it('does not claim improvement for missing or equal values', () => {
    expect(kpiDifference(null, 100, latency)).toBe('Difference not available')
    expect(kpiDifference(100, 100, latency)).toBe('No change')
    expect(kpiDifference(10, 0, latency)).toContain('percentage not available')
  })
})

it('does not arbitrarily tint one of two tied extrema', () => {
  expect(bestWorst([10, 10, 30], 'lower')).toEqual({ best: -1, worst: 2 })
  expect(bestWorst([10, 30, 30], 'lower')).toEqual({ best: 0, worst: -1 })
})

describe('acceptance threshold cells', () => {
  const sim = { engine: 'sim', passed: true, kpis: { p95_ms: 200, tps_avg: 50 } }
  it('judges the simulator in each metric direction and leaves real engines to their verdict', () => {
    const input = { summary: sim, thresholds: { p95_ms: 250, tps_avg: 60 }, collected: sim }
    expect(thresholdCell('p95_ms', input)).toEqual({ kind: 'limit', limit: 250, met: true, assessedBy: null })
    expect(thresholdCell('tps_avg', input)).toEqual({ kind: 'limit', limit: 60, met: false, assessedBy: null })
    const outcome = { ...sim, outcome: { assessment: { status: 'passed' } } } as unknown as TestResultSummary
    expect(thresholdCell('p95_ms', { summary: outcome, thresholds: { p95_ms: 100 }, collected: outcome })).toMatchObject({ met: null, assessedBy: 'Simulated engine' })
  })
  it('does not judge an execution that did not succeed', () => {
    expect(thresholdCell('p95_ms', { summary: sim, thresholds: { p95_ms: 100 }, collected: undefined })).toMatchObject({ met: null })
  })
  it('distinguishes no result, no limit and LoadRunner\'s own SLA settings', () => {
    expect(thresholdCell('p95_ms', { summary: undefined, thresholds: undefined, collected: undefined })).toEqual({ kind: 'unavailable' })
    expect(thresholdCell('p95_ms', { summary: sim, thresholds: { tps_avg: 1 }, collected: sim })).toEqual({ kind: 'none' })
    const lr = { engine: 'loadrunner', passed: true }
    expect(thresholdCell('p95_ms', { summary: lr, thresholds: undefined, collected: lr })).toEqual({ kind: 'engine-native', engine: 'LoadRunner' })
  })
  it('compares limits only across runs with a collected result', () => {
    const run = (p95?: number) => ({ summary: sim, thresholds: p95 === undefined ? undefined : { p95_ms: p95 }, collected: sim })
    expect(thresholdsDiffer('p95_ms', [run(250), run(250)])).toBe(false)
    expect(thresholdsDiffer('p95_ms', [run(250), run(400)])).toBe(true)
    expect(thresholdsDiffer('p95_ms', [run(250), run()])).toBe(true)
    expect(thresholdsDiffer('p95_ms', [run(250), { summary: undefined, thresholds: undefined, collected: undefined }])).toBe(false)
  })
  it('flags a simulated failure no limit explains', () => {
    const failed = { ...sim, passed: false }
    expect(unexplainedSimFailure({ summary: failed, thresholds: { p95_ms: 250 }, collected: failed })).toBe(true)
    expect(unexplainedSimFailure({ summary: failed, thresholds: { tps_avg: 60 }, collected: failed })).toBe(false)
    const real = { ...failed, engine: 'apex_load' }
    expect(unexplainedSimFailure({ summary: real, thresholds: { p95_ms: 250 }, collected: real })).toBe(false)
  })
})

describe('completed summaries and exact limits', () => {
  it('counts a failed execution only when the engine itself says Failed', () => {
    const failed = { engine: 'sim', passed: false }
    expect(completedSummary({ status: 'succeeded', test_summary: failed })).toBe(failed)
    expect(completedSummary({ status: 'failed', test_summary: failed })).toBe(failed)
    expect(completedSummary({ status: 'failed', test_summary: { engine: 'sim', passed: true } })).toBeUndefined()
    expect(completedSummary({ status: 'aborted', test_summary: failed })).toBeUndefined()
    expect(completedSummary({ status: 'failed' })).toBeUndefined()
  })
  it('keeps every digit of the applied limit', () => {
    expect(formatLimit('p95_ms', 211.9)).toBe('211.9 ms')
    expect(formatLimit('p95_ms', 300)).toBe('300 ms')
    expect(formatLimit('tps_avg', 22.25)).toBe('22.25 TPS')
    expect(formatLimit('error_rate', 0.05)).toBe('5.00%')
    expect(formatLimit('error_rate', 0.0125)).toBe('1.25%')
    expect(formatLimit('error_rate', 0.01255)).toBe('1.255%')
  })
})
