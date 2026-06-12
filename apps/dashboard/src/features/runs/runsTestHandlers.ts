/**
 * MSW handlers + fixtures for GET /v1/pipelines, registered per-test via
 * `server.use(...)` (the shared src/test/server.ts stays untouched).
 */
import { http, HttpResponse, delay } from 'msw'

import { PHASE_NAMES } from '@apex/pipeline-events'

import type {
  PipelineListResponse,
  PipelineSummary,
  PhaseStripEntry,
} from '@/api/hooks/usePipelines'

/** Full 7-segment strip, optionally overriding individual phases. */
export function makeStrip(
  overrides: Partial<Record<(typeof PHASE_NAMES)[number], Partial<PhaseStripEntry>>> = {},
): PhaseStripEntry[] {
  return PHASE_NAMES.map((phase) => ({
    phase,
    status: 'pending',
    attempt: null,
    ...overrides[phase],
  }))
}

export const RUN_BUSY: PipelineSummary = {
  thread_id: 'run-busy-1',
  title: 'Checkout latency regression',
  project_id: 'proj-alpha',
  app_id: 'app-storefront',
  thread_status: 'busy',
  current_phase: 'execution',
  phase_strip: makeStrip({
    story_analysis: { status: 'succeeded', attempt: 1 },
    test_planning: { status: 'succeeded', attempt: 1 },
    env_triage: { status: 'skipped' },
    script_scenario: { status: 'succeeded', attempt: 2 },
    execution: { status: 'running', attempt: 1 },
  }),
  engine: { engine: 'apexload', external_run_id: 'al-ext-42' },
  created_at: '2026-06-12T08:00:00Z',
  updated_at: '2026-06-12T08:30:00Z',
  pending_gate: null,
}

export const RUN_GATED: PipelineSummary = {
  thread_id: 'run-gated-2',
  title: 'Nightly soak',
  project_id: 'proj-alpha',
  app_id: null,
  thread_status: 'interrupted',
  current_phase: 'test_planning',
  phase_strip: makeStrip({
    story_analysis: { status: 'succeeded', attempt: 1 },
    test_planning: { status: 'awaiting_prompt_review', attempt: 1 },
  }),
  engine: null,
  created_at: '2026-06-11T22:00:00Z',
  updated_at: '2026-06-12T01:15:00Z',
  pending_gate: { interrupt_id: 'int-7', kind: 'prompt_review', phase: 'test_planning' },
}

export const PIPELINES_FIXTURE: PipelineSummary[] = [RUN_BUSY, RUN_GATED]

/** n synthetic runs (for full-page pagination scenarios). */
export function makeSummaries(n: number, offset = 0): PipelineSummary[] {
  return Array.from({ length: n }, (_, i) => ({
    thread_id: `run-${offset + i}`,
    title: `Run ${offset + i}`,
    project_id: 'proj-alpha',
    app_id: null,
    thread_status: 'idle',
    current_phase: null,
    phase_strip: makeStrip({ story_analysis: { status: 'succeeded', attempt: 1 } }),
    engine: null,
    created_at: '2026-06-10T00:00:00Z',
    updated_at: '2026-06-10T01:00:00Z',
    pending_gate: null,
  }))
}

export interface CapturedPipelinesRequest {
  queries: URLSearchParams[]
  last: () => URLSearchParams | undefined
}

/**
 * Handler answering GET /v1/pipelines with `items`, echoing limit/offset from
 * the request. Returns a capture object recording every request's query params.
 */
export function pipelinesHandler(
  items: PipelineSummary[],
  options: { total?: number; failFirst?: boolean } = {},
) {
  const captured: CapturedPipelinesRequest = {
    queries: [],
    last: () => captured.queries[captured.queries.length - 1],
  }
  let failed = false
  const handler = http.get('*/v1/pipelines', ({ request }) => {
    const url = new URL(request.url)
    captured.queries.push(url.searchParams)
    if (options.failFirst && !failed) {
      failed = true
      return HttpResponse.json({ detail: 'projection unavailable' }, { status: 500 })
    }
    const body: PipelineListResponse = {
      items,
      limit: Number(url.searchParams.get('limit') ?? '20'),
      offset: Number(url.searchParams.get('offset') ?? '0'),
      ...(options.total !== undefined ? { total: options.total } : {}),
    }
    return HttpResponse.json(body)
  })
  return { handler, captured }
}

/** Handler that never resolves — pins the page in its loading state. */
export function pipelinesNeverResolves() {
  return http.get('*/v1/pipelines', async () => {
    await delay('infinite')
    return HttpResponse.json({ items: [], limit: 25, offset: 0 })
  })
}
