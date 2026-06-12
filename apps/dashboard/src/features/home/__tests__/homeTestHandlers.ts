/**
 * MSW handlers + fixtures for the Home dashboard tests, registered per-test
 * via server.use(...) (the shared src/test/server.ts defaults stay untouched).
 *
 * The fleet handler emulates the backend's ?status= filter so ONE handler
 * serves both the page's unfiltered fleet query and the approvals-inbox /
 * sidebar-badge query ({status:'interrupted'}).
 */
import { http, HttpResponse } from 'msw'

import type { UsageAnalytics } from '@/api/hooks/useAnalytics'
import type { DraftRead } from '@/api/hooks/useDrafts'
import type { PipelineSummary } from '@/api/hooks/usePipelines'
import { makeStrip } from '@/features/runs/runsTestHandlers'

/** Dynamic timestamps keep relative-age rendering independent of the clock. */
export function minutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString()
}

export const GATED_OLD: PipelineSummary = {
  thread_id: 'run-gated-old',
  title: 'Oldest gated run',
  project_id: 'proj-alpha',
  app_id: null,
  thread_status: 'interrupted',
  current_phase: 'test_planning',
  phase_strip: makeStrip({
    story_analysis: { status: 'succeeded', attempt: 1 },
    test_planning: { status: 'awaiting_prompt_review', attempt: 1 },
  }),
  engine: null,
  created_at: minutesAgo(120),
  updated_at: minutesAgo(60),
  pending_gate: { interrupt_id: 'int-old', kind: 'prompt_review', phase: 'test_planning' },
}

export const GATED_NEW: PipelineSummary = {
  thread_id: 'run-gated-new',
  title: 'Newest gated run',
  project_id: 'proj-alpha',
  app_id: null,
  thread_status: 'interrupted',
  current_phase: 'reporting',
  phase_strip: makeStrip({
    story_analysis: { status: 'succeeded', attempt: 1 },
    reporting: { status: 'awaiting_output_review', attempt: 1 },
  }),
  engine: null,
  created_at: minutesAgo(90),
  updated_at: minutesAgo(5),
  pending_gate: { interrupt_id: 'int-new', kind: 'phase_review', phase: 'reporting' },
}

export const BUSY_RUN: PipelineSummary = {
  thread_id: 'run-busy',
  title: 'Checkout latency soak',
  project_id: 'proj-alpha',
  app_id: 'app-storefront',
  thread_status: 'busy',
  current_phase: 'execution',
  phase_strip: makeStrip({
    story_analysis: { status: 'succeeded', attempt: 1 },
    execution: { status: 'running', attempt: 1 },
  }),
  engine: { engine: 'apexload', external_run_id: 'al-1' },
  created_at: minutesAgo(30),
  updated_at: minutesAgo(2),
  pending_gate: null,
}

export const FAILED_RUN: PipelineSummary = {
  thread_id: 'run-failed',
  title: 'Broken nightly run',
  project_id: 'proj-alpha',
  app_id: null,
  thread_status: 'error',
  current_phase: 'execution',
  phase_strip: makeStrip({
    story_analysis: { status: 'succeeded', attempt: 1 },
    execution: { status: 'failed', attempt: 2 },
  }),
  engine: null,
  created_at: minutesAgo(200),
  updated_at: minutesAgo(10),
  pending_gate: null,
}

export const IDLE_RUN: PipelineSummary = {
  thread_id: 'run-idle',
  title: 'Completed smoke run',
  project_id: 'proj-alpha',
  app_id: null,
  thread_status: 'idle',
  current_phase: null,
  phase_strip: makeStrip({ story_analysis: { status: 'succeeded', attempt: 1 } }),
  engine: null,
  created_at: minutesAgo(400),
  updated_at: minutesAgo(300),
  pending_gate: null,
}

export const FLEET_FIXTURE: PipelineSummary[] = [
  GATED_OLD,
  GATED_NEW,
  BUSY_RUN,
  FAILED_RUN,
  IDLE_RUN,
]

/**
 * GET /v1/pipelines emulating the backend's ?status= filter, so the page's
 * unfiltered fleet query and the inbox's {status:'interrupted'} query both
 * answer from the same fixture.
 */
export function fleetHandler(items: PipelineSummary[]) {
  return http.get('*/v1/pipelines', ({ request }) => {
    const url = new URL(request.url)
    const status = url.searchParams.get('status')
    const filtered = status ? items.filter((run) => run.thread_status === status) : items
    return HttpResponse.json({
      items: filtered,
      limit: Number(url.searchParams.get('limit') ?? '20'),
      offset: 0,
    })
  })
}

export const USAGE_FIXTURE: UsageAnalytics = {
  window: { from: minutesAgo(7 * 24 * 60), to: minutesAgo(0), bucket: 'day' },
  totals: { events: 960, errors: 48, by_surface: { v1: 900, graph: 60 } },
  buckets: [],
  top_actions: [],
  runs: { phases_succeeded: 42, phases_failed: 3 },
}

export function usageHandler(body: UsageAnalytics = USAGE_FIXTURE) {
  return http.get('*/v1/analytics/usage', () => HttpResponse.json(body))
}

export function usageFailsHandler() {
  return http.get('*/v1/analytics/usage', () =>
    HttpResponse.json({ detail: 'analytics unavailable' }, { status: 500 }),
  )
}

export const DRAFTS_FIXTURE: DraftRead[] = [
  {
    id: 'draft-1',
    title: 'Black Friday load test',
    payload: {},
    project_id: 'proj-alpha',
    created_by: 'dash-ops',
    created_at: minutesAgo(180),
    updated_at: minutesAgo(15),
  },
  {
    id: 'draft-2',
    title: 'API soak draft',
    payload: {},
    project_id: 'proj-alpha',
    created_by: 'dash-ops',
    created_at: minutesAgo(600),
    updated_at: minutesAgo(120),
  },
]

export function draftsHandler(body: DraftRead[] = DRAFTS_FIXTURE) {
  return http.get('*/v1/drafts', () => HttpResponse.json(body))
}
