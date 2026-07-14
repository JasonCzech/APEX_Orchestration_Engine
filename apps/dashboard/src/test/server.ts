import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'

import type { SystemInfo } from '@/api/apexClient'

/** Canonical happy-path payload for GET /v1/system/info (admin consumer). */
export const SYSTEM_INFO: SystemInfo = {
  name: 'APEX Orchestration Engine',
  version: '0.0.0-test',
  environment: 'test',
  features: { engines: true, documents: true },
  consumer: {
    name: 'Dash Ops',
    role: 'admin',
    scopes: [{ project_id: 'proj-alpha', app_id: null }],
  },
}

export function systemInfoWith(overrides: Partial<SystemInfo>): SystemInfo {
  return { ...SYSTEM_INFO, ...overrides }
}

/** Zero-usage analytics payload (Home panel reads GET /v1/analytics/usage). */
const EMPTY_USAGE = {
  window: { from: '2026-06-05T00:00:00Z', to: '2026-06-12T00:00:00Z', bucket: 'day' },
  totals: { events: 0, errors: 0, by_surface: {} },
  buckets: [],
  top_actions: [],
  runs: { phases_succeeded: 0, phases_failed: 0 },
}

const EMPTY_AGENT_ANALYTICS = {
  window: {
    from: '2026-06-05T00:00:00Z',
    to: '2026-06-12T00:00:00Z',
    bucket: 'day',
    group_by: 'model',
  },
  totals: {
    events: 0,
    errors: 0,
    input_tokens: 0,
    output_tokens: 0,
    total_tokens: 0,
    cache_read_tokens: 0,
    cache_creation_tokens: 0,
    reasoning_tokens: 0,
    cost_usd: null,
    avg_latency_ms: null,
    p95_latency_ms: null,
    runs: 0,
    agents: 0,
    models: 0,
  },
  breakdown: [],
  series: [],
  page: { limit: 20, offset: 0, total: 0 },
  cost_visible: false,
}

export const handlers = [
  http.get('*/v1/system/info', () => HttpResponse.json(SYSTEM_INFO)),
  http.get('*/v1/auth/me', () =>
    HttpResponse.json({
      principal_kind: 'api_consumer',
      principal_id: 'cons-ops',
      name: SYSTEM_INFO.consumer.name,
      consumer_type: 'dashboard',
      role: SYSTEM_INFO.consumer.role,
      scopes: SYSTEM_INFO.consumer.scopes,
      is_unscoped: false,
      mfa_required: false,
      step_up_required: false,
      capabilities: {},
    }),
  ),
  // The shell's Approvals badge (Sidebar -> useApprovalsInbox, D3) polls the
  // pipelines list on every authenticated mount; default to an empty fleet so
  // shell-level tests stay quiet. Tests that need rows register their own
  // handler via server.use(...), which takes precedence.
  http.get('*/v1/pipelines', () => HttpResponse.json({ items: [], limit: 100, offset: 0 })),
  http.get('*/v1/pipelines/:threadId', ({ params }) =>
    HttpResponse.json(
      { detail: `pipeline ${String(params['threadId'])} is not configured in this test` },
      { status: 404 },
    ),
  ),
  http.get('*/v1/pipelines/:threadId/phases/:phase/prompt-review', ({ params }) =>
    HttpResponse.json({
      system: `Test system prompt for ${String(params['phase'])}.`,
      phase_prompt: `Test phase prompt for ${String(params['phase'])}.`,
      application: null,
      additional_context: '',
      source: { origin: 'catalog', ref: `phase/${String(params['phase'])}@test` },
      updated_at: '2026-06-01T00:00:00+00:00',
      updated_by: 'system',
    }),
  ),
  // The Home dashboard (/, D7) additionally reads drafts + usage analytics on
  // mount; default to empty so tests that merely pass through '/' stay quiet.
  http.get('*/v1/drafts', () => HttpResponse.json([])),
  http.get('*/v1/analytics/usage', () => HttpResponse.json(EMPTY_USAGE)),
  http.get('*/v1/analytics/agents', () => HttpResponse.json(EMPTY_AGENT_ANALYTICS)),
]

export const server = setupServer(...handlers)
