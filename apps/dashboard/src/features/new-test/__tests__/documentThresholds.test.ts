import { describe, expect, it } from 'vitest'
import { documentThresholdMismatches } from '../workload'

describe('documentThresholdMismatches', () => {
  const load = { slas: { p95_ms: 500, error_rate: 0.05 } }

  it('flags documented figures that differ from Config', () => {
    expect(documentThresholdMismatches([{ name: 'runbook.md', text: 'Acceptance: P95 latency must stay under 300 ms; error rate below 1%.' }], load)).toEqual([
      { document: 'runbook.md', key: 'p95_ms', label: 'P95', documented: '300 ms', configured: '500 ms' },
      { document: 'runbook.md', key: 'error_rate', label: 'Error rate', documented: '1%', configured: '5%' },
    ])
  })

  it('does not flag a metric when any mention agrees with Config', () => {
    // Reports quote observed values and older targets beside the current one.
    const report = 'Baseline run: p95 812 ms. Old SLA p95 800 ms; new SLA p95 500 ms.'
    expect(documentThresholdMismatches([{ name: 'report.md', text: report }], load)).toEqual([])
  })

  it('stays quiet when documents agree, say nothing, or Config omits the threshold', () => {
    expect(documentThresholdMismatches([{ name: 'a.md', text: 'p95 <= 500ms and error rate 5%' }], load)).toEqual([])
    expect(documentThresholdMismatches([{ name: 'b.md', text: 'Peak is 85 TPS for 30 minutes.' }], load)).toEqual([])
    expect(documentThresholdMismatches([{ name: 'c.md', text: 'P95 300 ms' }], { slas: {} })).toEqual([])
    expect(documentThresholdMismatches([{ name: 'd.md', text: null }], load)).toEqual([])
  })
})
