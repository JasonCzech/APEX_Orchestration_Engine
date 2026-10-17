import { describe, expect, it } from 'vitest'

import type { PipelineSummary } from '@/api/hooks/usePipelines'
import { makeStrip } from '@/features/runs/runsTestHandlers'

import { failedRuns, passedTests, resultCounts } from './homeLogic'

function idleRun(id: string, status: string): PipelineSummary {
  return {
    thread_id: id,
    title: id,
    project_id: null,
    app_id: null,
    thread_status: 'idle',
    current_phase: null,
    phase_strip: makeStrip({ execution: { status, attempt: 1 } }),
    engine: null,
    created_at: null,
    updated_at: null,
    pending_gate: null,
  }
}

describe('home verdict totals', () => {
  it('counts recorded test results without deriving passes from phase completion', () => {
    const runs = [
      { ...idleRun('passed', 'succeeded'), execution_result: 'passed' as const },
      { ...idleRun('failed-sla', 'succeeded'), execution_result: 'failed' as const },
      idleRun('unknown', 'succeeded'),
      idleRun('aborted', 'aborted'),
    ]
    expect(passedTests(runs)).toBe(1)
    expect(failedRuns(runs).map((run) => run.thread_id)).toEqual(['failed-sla'])
  })
})

it('preserves inconclusive and not-assessed outcomes rather than merging them into missing data', () => {
  expect(resultCounts([
    { ...idleRun('inconclusive', 'succeeded'), execution_result: 'inconclusive' },
    { ...idleRun('not-assessed', 'succeeded'), execution_result: 'not_assessed' },
    { ...idleRun('live', 'running'), execution_result: 'failed' },
  ])).toEqual({ Passed: 0, Failed: 0, 'Not available': 1, Inconclusive: 1, 'Not assessed': 1 })
})
