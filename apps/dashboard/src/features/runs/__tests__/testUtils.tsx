/**
 * Run-detail test fixtures + msw handlers (registered per-test via server.use)
 * and a memory-router harness mirroring the intended route wiring (see
 * ../ROUTES.md — the real wiring is the integrator's).
 */
import { render } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import type { PipelineDetail } from '@/api/hooks/useThreadState'
import { ArtifactViewerPage } from '@/features/artifacts/ArtifactViewerPage'
import { RunDetailPage } from '@/features/runs/RunDetailPage'
import { TimelinePage } from '@/features/runs/TimelinePage'
import { createTestQueryClient } from '@/test/render'

export const THREAD_ID = 'thread-1'

const TRANSCRIPT = (phase: string, attempt: number) => ({
  id: `${phase}-a${attempt}-transcript`,
  kind: 'transcript',
  name: `${phase} transcript (attempt ${attempt})`,
  uri: `memory://transcripts/${phase}/attempt-${attempt}`,
  media_type: 'text/plain',
  summary: null,
})

export const REPORT_JSON_BODY = {
  engine: 'sim',
  kpis: { tps_avg: 42.5, p95_ms: 212.0, error_rate: 0.0042, vusers_peak: 50 },
}

const VALUES = {
  title: 'Checkout latency regression',
  current_phase: 'reporting',
  phases_plan: [
    'story_analysis',
    'test_planning',
    'env_triage',
    'script_scenario',
    'execution',
    'reporting',
    'postmortem',
  ],
  phase_results: {
    story_analysis: {
      phase: 'story_analysis',
      status: 'succeeded',
      attempt: 1,
      started_at: '2026-06-01T10:00:00+00:00',
      ended_at: '2026-06-01T10:00:04+00:00',
      duration_s: 4.2,
      summary: 'Parsed the checkout story.\n\nScoped three user flows for load coverage.',
      reasoning_digest: 'Focused on checkout and cart flows; payments excluded.',
      artifact_ids: ['story_analysis-a1-transcript'],
      approvals: [
        {
          id: 'ap-1',
          gate: 'prompt_review',
          action: 'approve',
          actor: 'ops@apex',
          at: '2026-06-01T10:00:01+00:00',
        },
      ],
      resolved_prompt: {
        system: 'You are the story analysis agent.',
        user: 'Analyze APEX-101 for load-test scope.',
      },
      resolved_prompt_source: { origin: 'catalog', ref: 'story_analysis@v3' },
      transcript_ref: TRANSCRIPT('story_analysis', 1),
    },
    test_planning: {
      phase: 'test_planning',
      status: 'succeeded',
      attempt: 2,
      started_at: '2026-06-01T10:01:00+00:00',
      ended_at: '2026-06-01T10:02:15+00:00',
      duration_s: 75.4,
      summary: 'Planned 4 scenarios against the staging cluster.',
      warnings: ['Plan exceeded the latency budget; trimmed scenario set.'],
      artifact_ids: ['test_planning-a2-transcript'],
      approvals: [
        {
          id: 'ap-2',
          gate: 'phase_review',
          action: 'approve',
          actor: 'ops@apex',
          at: '2026-06-01T10:02:00+00:00',
        },
      ],
      transcript_ref: TRANSCRIPT('test_planning', 2),
    },
    env_triage: {
      phase: 'env_triage',
      status: 'skipped',
      attempt: 1,
      started_at: '2026-06-01T10:03:00+00:00',
      ended_at: '2026-06-01T10:03:01+00:00',
      duration_s: 1.0,
    },
    script_scenario: {
      phase: 'script_scenario',
      status: 'succeeded',
      attempt: 1,
      started_at: '2026-06-01T10:04:00+00:00',
      ended_at: '2026-06-01T10:04:30+00:00',
      duration_s: 30.0,
      summary: 'Generated the checkout script and ramp scenario.',
      artifact_ids: ['script_scenario-a1-transcript'],
      transcript_ref: TRANSCRIPT('script_scenario', 1),
    },
    execution: {
      phase: 'execution',
      status: 'succeeded',
      attempt: 1,
      started_at: '2026-06-01T10:05:00+00:00',
      ended_at: '2026-06-01T10:10:12+00:00',
      duration_s: 312.0,
      summary: 'Load test completed within SLA.',
      artifact_ids: ['exec-report', 'exec-archive', 'execution-a1-transcript'],
      engine: 'sim',
      engine_started_at: '2026-06-01T10:05:10+00:00',
      engine_handle: { engine: 'sim', connection_id: 'conn-sim', external_run_id: 'sim-123' },
      test_summary: {
        engine: 'sim',
        passed: true,
        kpis: { tps_avg: 42.5, p95_ms: 212.0, error_rate: 0.0042, vusers_peak: 50 },
      },
      transcript_ref: TRANSCRIPT('execution', 1),
    },
    reporting: {
      phase: 'reporting',
      status: 'running',
      attempt: 1,
      started_at: '2026-06-01T10:11:00+00:00',
    },
  },
  artifacts: [
    TRANSCRIPT('story_analysis', 1),
    TRANSCRIPT('test_planning', 2),
    TRANSCRIPT('script_scenario', 1),
    TRANSCRIPT('execution', 1),
    {
      id: 'exec-report',
      kind: 'report',
      name: 'load-report.json',
      uri: 'memory://reports/thread-1/load-report.json',
      media_type: 'application/json',
      summary: 'Normalized KPI summary',
    },
    {
      id: 'exec-archive',
      kind: 'archive',
      name: 'results.zip',
      uri: 'memory://bin/thread-1/results.zip',
      media_type: 'application/octet-stream',
      summary: 'Raw engine results',
    },
  ],
  dialogue: [
    {
      id: 'd-1',
      phase: 'test_planning',
      role: 'operator',
      content: 'Tighten the ramp to 5 minutes.',
      at: '2026-06-01T10:01:30+00:00',
    },
    {
      id: 'd-2',
      phase: 'test_planning',
      role: 'agent',
      content: 'Ramp tightened to 5m; scenarios re-balanced.',
      at: '2026-06-01T10:01:40+00:00',
    },
  ],
  engine_handle: { engine: 'sim', connection_id: 'conn-sim', external_run_id: 'sim-123' },
}

export const PIPELINE_DETAIL: PipelineDetail = {
  thread_id: THREAD_ID,
  title: 'Checkout latency regression',
  project_id: 'proj-alpha',
  app_id: 'app-storefront',
  thread_status: 'busy',
  current_phase: 'reporting',
  phase_strip: [
    { phase: 'story_analysis', status: 'succeeded', attempt: 1 },
    { phase: 'test_planning', status: 'succeeded', attempt: 2 },
    { phase: 'env_triage', status: 'skipped', attempt: 1 },
    { phase: 'script_scenario', status: 'succeeded', attempt: 1 },
    { phase: 'execution', status: 'succeeded', attempt: 1 },
    { phase: 'reporting', status: 'running', attempt: 1 },
    { phase: 'postmortem', status: 'none', attempt: null },
  ],
  engine: { engine: 'sim', external_run_id: 'sim-123' },
  created_at: '2026-06-01T09:59:00+00:00',
  updated_at: '2026-06-01T10:11:00+00:00',
  pending_gate: null,
  values: VALUES,
  interrupts: [],
}

/** Same thread, but interrupted on a phase_review gate (RunRail banner case). */
export const PIPELINE_DETAIL_INTERRUPTED: PipelineDetail = {
  ...PIPELINE_DETAIL,
  thread_status: 'interrupted',
  pending_gate: { interrupt_id: 'int-1', kind: 'phase_review', phase: 'reporting' },
  interrupts: [
    {
      interrupt_id: 'int-1',
      kind: 'phase_review',
      phase: 'reporting',
      payload: {
        kind: 'phase_review',
        phase: 'reporting',
        actions: ['approve', 'revise', 'discuss', 'abort'],
      },
    },
  ],
}

export function pipelineDetailHandler(detail: PipelineDetail = PIPELINE_DETAIL) {
  return http.get(`*/v1/pipelines/${THREAD_ID}`, () => HttpResponse.json(detail))
}

export const artifactHandlers = [
  http.get('*/v1/artifacts/reports/thread-1/load-report.json', () =>
    HttpResponse.json(REPORT_JSON_BODY),
  ),
  http.get(
    '*/v1/artifacts/bin/thread-1/results.zip',
    () =>
      new HttpResponse(new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0x00, 0x01]).buffer, {
        headers: { 'Content-Type': 'application/octet-stream' },
      }),
  ),
]

/**
 * Memory router mirroring the route wiring this feature documents in ROUTES.md.
 * Kept local so D0's appRoutes (placeholder pages) stay untouched until the
 * integrator wires the real pages.
 */
export function renderRunRoutes(
  initialEntries: string[],
  queryClient: QueryClient = createTestQueryClient(),
) {
  const router = createMemoryRouter(
    [
      { path: '/runs/:threadId', element: <RunDetailPage /> },
      { path: '/runs/:threadId/phases/:phase', element: <RunDetailPage /> },
      { path: '/runs/:threadId/timeline', element: <TimelinePage /> },
      { path: '/runs/:threadId/artifacts/:name', element: <ArtifactViewerPage /> },
    ],
    { initialEntries },
  )
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...result, router, queryClient }
}
