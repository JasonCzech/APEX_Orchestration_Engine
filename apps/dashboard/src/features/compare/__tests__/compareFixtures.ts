/**
 * MSW handlers + fixtures for the compare-view tests: two fully-populated
 * run snapshots (facade GET /v1/pipelines/{thread_id}) with diverging phase
 * durations and engine KPIs, a third minimal run for the [Add run] picker,
 * and a recent-runs list handler (GET /v1/pipelines).
 */
import { http, HttpResponse } from 'msw'

import type { PipelineListResponse, PipelineSummary } from '@/api/hooks/usePipelines'
import type { PipelineDetail } from '@/api/hooks/useThreadState'

export const RUN_A_ID = 'cmp-a'
export const RUN_B_ID = 'cmp-b'
export const RUN_C_ID = 'cmp-c'

interface DetailSeed {
  threadId: string
  title: string
  threadStatus: string
  engine: string | null
  values: Record<string, unknown>
}

function makeDetail({ threadId, title, threadStatus, engine, values }: DetailSeed): PipelineDetail {
  return {
    thread_id: threadId,
    title,
    project_id: 'proj-alpha',
    app_id: 'app-storefront',
    thread_status: threadStatus,
    current_phase: 'execution',
    phase_strip: [],
    engine: engine ? { engine, external_run_id: `${engine}-ext-1` } : null,
    created_at: '2026-06-01T09:00:00Z',
    updated_at: '2026-06-01T10:00:00Z',
    pending_gate: null,
    values,
    interrupts: [],
  } as PipelineDetail
}

/**
 * Run A — the faster, passing run. story_analysis 4s, test_planning 60s,
 * execution 300s; KPIs better across the board.
 */
export const COMPARE_DETAIL_A: PipelineDetail = makeDetail({
  threadId: RUN_A_ID,
  title: 'Checkout latency regression',
  threadStatus: 'idle',
  engine: 'sim',
  values: {
    title: 'Checkout latency regression',
    current_phase: 'execution',
    phase_results: {
      story_analysis: {
        phase: 'story_analysis',
        status: 'succeeded',
        attempt: 1,
        duration_s: 4,
        artifact_ids: ['a-sa-1'],
        warnings: [],
      },
      test_planning: {
        phase: 'test_planning',
        status: 'succeeded',
        attempt: 1,
        duration_s: 60,
        artifact_ids: ['a-tp-1', 'a-tp-2'],
        warnings: ['Plan trimmed to fit the latency budget.'],
      },
      execution: {
        phase: 'execution',
        status: 'succeeded',
        attempt: 1,
        duration_s: 300,
        artifact_ids: ['a-ex-report'],
        warnings: [],
        engine: 'sim',
        test_summary: {
          engine: 'sim',
          passed: true,
          kpis: { tps_avg: 50, p95_ms: 200, error_rate: 0.004, vusers_peak: 100 },
        },
      },
    },
  },
})

/**
 * Run B — slower and failing. story_analysis 10s (>1.5x A's 4s → amber),
 * test_planning 70s (within 1.5x of A's 60s → no highlight), execution failed
 * with worse KPIs.
 */
export const COMPARE_DETAIL_B: PipelineDetail = makeDetail({
  threadId: RUN_B_ID,
  title: 'Nightly soak',
  threadStatus: 'error',
  engine: 'apexload',
  values: {
    title: 'Nightly soak',
    current_phase: 'execution',
    phase_results: {
      story_analysis: {
        phase: 'story_analysis',
        status: 'succeeded',
        attempt: 2,
        duration_s: 10,
        artifact_ids: ['b-sa-1'],
        warnings: [],
      },
      test_planning: {
        phase: 'test_planning',
        status: 'succeeded',
        attempt: 1,
        duration_s: 70,
        artifact_ids: [],
        warnings: [],
      },
      execution: {
        phase: 'execution',
        status: 'failed',
        attempt: 1,
        duration_s: 120,
        artifact_ids: ['b-ex-report'],
        warnings: ['SLA breach: p95 above budget.'],
        engine: 'apexload',
        test_summary: {
          engine: 'apexload',
          passed: false,
          kpis: { tps_avg: 30, p95_ms: 350, error_rate: 0.02, vusers_peak: 80 },
        },
      },
    },
  },
})

/** Run C — minimal third run, used by the [Add run] picker test. */
export const COMPARE_DETAIL_C: PipelineDetail = makeDetail({
  threadId: RUN_C_ID,
  title: 'Throughput probe',
  threadStatus: 'idle',
  engine: null,
  values: {
    title: 'Throughput probe',
    phase_results: {
      story_analysis: {
        phase: 'story_analysis',
        status: 'succeeded',
        attempt: 1,
        duration_s: 5,
        artifact_ids: [],
        warnings: [],
      },
    },
  },
})

export const COMPARE_DETAILS = [COMPARE_DETAIL_A, COMPARE_DETAIL_B, COMPARE_DETAIL_C]

function summaryOf(detail: PipelineDetail): PipelineSummary {
  return {
    thread_id: detail.thread_id,
    title: detail.title,
    project_id: detail.project_id,
    app_id: detail.app_id,
    thread_status: detail.thread_status,
    current_phase: detail.current_phase,
    phase_strip: [],
    engine: detail.engine ?? null,
    created_at: detail.created_at,
    updated_at: detail.updated_at,
    pending_gate: null,
  } as PipelineSummary
}

export const COMPARE_SUMMARIES: PipelineSummary[] = COMPARE_DETAILS.map(summaryOf)

/** Facade snapshot handler answering for any of the given details by thread id. */
export function compareDetailHandler(details: PipelineDetail[] = COMPARE_DETAILS) {
  return http.get('*/v1/pipelines/:threadId', ({ params }) => {
    const detail = details.find((candidate) => candidate.thread_id === params['threadId'])
    return detail
      ? HttpResponse.json(detail)
      : HttpResponse.json({ detail: 'thread not found' }, { status: 404 })
  })
}

/** Recent-runs list handler for the [Add run] picker. */
export function compareListHandler(items: PipelineSummary[] = COMPARE_SUMMARIES) {
  return http.get('*/v1/pipelines', () => {
    const body: PipelineListResponse = { items, limit: 10, offset: 0 }
    return HttpResponse.json(body)
  })
}
