import { describe, expect, it } from 'vitest'
import type { AgentAnalyticsSeriesPoint } from '@/api/hooks/useAgentAnalytics'
import { aggregateMeasurements, measurementSeries, metricValue, rankedMeasurements } from './analyticsMeasurements'

const point = (changes: Partial<AgentAnalyticsSeriesPoint> = {}): AgentAnalyticsSeriesPoint => ({
  bucket_start: '2026-10-17T00:00:00Z', key: 'worker', events: 100, latency_samples: 1,
  errors: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0, cache_read_tokens: 0,
  cache_creation_tokens: 0, reasoning_tokens: 0, cost_usd: null, avg_latency_ms: 100,
  p95_latency_ms: 900, runs: 1, ...changes,
})

describe('recorded analytics measurements', () => {
  it('weights averages by actual timing samples, never all events', () => {
    const rows = [point(), point({ latency_samples: 3, avg_latency_ms: 900 })]
    expect(aggregateMeasurements(rows, 'latency')).toBe(700)
    expect(rankedMeasurements(rows, 'latency')).toEqual([{ key: 'worker', value: 700 }])
    expect(measurementSeries(rows, 'latency', 'date').series[0]).toEqual({ bucket_start: rows[0]!.bucket_start, total: 700 })
    expect(measurementSeries(rows, 'latency', 'date').keys).toEqual(['Average'])
    expect(measurementSeries(rows, 'tokens', 'date').keys).toEqual(['Total'])
  })

  it('does not replace a missing average with a percentile or combine legacy averages without counts', () => {
    expect(metricValue(point({ avg_latency_ms: null, p95_latency_ms: 1000 }), 'latency')).toBeNull()
    expect(aggregateMeasurements([point(), point({ latency_samples: null })], 'latency')).toBeNull()
    expect(aggregateMeasurements([point({ latency_samples: 0 })], 'latency')).toBeNull()
  })

  it('preserves measured zero and missing bucket gaps for each measure', () => {
    for (const measure of ['cost', 'latency'] as const) {
      const rows = [point({ avg_latency_ms: 0, cost_usd: 0 }), point({ bucket_start: '2026-10-18T00:00:00Z', avg_latency_ms: null, cost_usd: null, latency_samples: 0 })]
      expect(rankedMeasurements(rows, measure)).toEqual([{ key: 'worker', value: 0 }])
      expect(measurementSeries(rows, measure, 'model').series).toEqual([
        { bucket_start: rows[0]!.bucket_start, series_0: 0 },
        { bucket_start: rows[1]!.bucket_start, series_0: null },
      ])
    }
  })

  it('weights the Other series by timing samples too', () => {
    const rows = Array.from({ length: 5 }, (_, index) => point({ key: `top-${index}`, avg_latency_ms: 1000 + index }))
    rows.push(point({ key: 'other-a', avg_latency_ms: 100 }), point({ key: 'other-b', avg_latency_ms: 500, latency_samples: 3 }))
    expect(measurementSeries(rows, 'latency', 'model').series[0]).toMatchObject({ other: 400 })
  })

  it('rejects overflowing aggregates without printing nonfinite values', () => {
    expect(aggregateMeasurements([point({ avg_latency_ms: Number.MAX_VALUE, latency_samples: 2 })], 'latency')).toBeNull()
    expect(aggregateMeasurements([point({ cost_usd: Number.MAX_VALUE }), point({ cost_usd: Number.MAX_VALUE })], 'cost')).toBeNull()
    expect(aggregateMeasurements([point({ avg_latency_ms: 0, latency_samples: Number.MAX_SAFE_INTEGER }), point({ avg_latency_ms: 0 })], 'latency')).toBeNull()
  })
})
