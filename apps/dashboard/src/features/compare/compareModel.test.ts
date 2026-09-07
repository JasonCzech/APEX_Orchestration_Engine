import { describe, expect, it } from 'vitest'
import { COMPARE_KPIS, kpiDifference } from './compareModel'

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
