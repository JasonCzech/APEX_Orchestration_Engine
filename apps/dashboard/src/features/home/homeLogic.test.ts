import { describe, expect, it } from 'vitest'

import type { PipelineSummary } from '@/api/hooks/usePipelines'
import { makeStrip } from '@/features/runs/runsTestHandlers'

import { failedRuns, goVerdicts } from './homeLogic'

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
  it('does not count failed, aborted, or still-running idle threads as GO', () => {
    const runs = [
      idleRun('succeeded', 'succeeded'),
      idleRun('failed', 'failed'),
      idleRun('aborted', 'aborted'),
      idleRun('inconsistent', 'running'),
    ]

    expect(goVerdicts(runs)).toBe(1)
    expect(failedRuns(runs).map((run) => run.thread_id).sort()).toEqual([
      'aborted',
      'failed',
      'inconsistent',
    ])
  })
})
