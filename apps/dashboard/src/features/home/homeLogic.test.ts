import { describe, expect, it } from 'vitest'

import type { PipelineSummary } from '@/api/hooks/usePipelines'
import { makeStrip } from '@/features/runs/runsTestHandlers'

import { failedRuns, passedTests } from './homeLogic'

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
