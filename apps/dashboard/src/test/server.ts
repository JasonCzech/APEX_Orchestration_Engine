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

export const handlers = [
  http.get('*/v1/system/info', () => HttpResponse.json(SYSTEM_INFO)),
  // The shell's Approvals badge (Sidebar -> useApprovalsInbox, D3) polls the
  // pipelines list on every authenticated mount; default to an empty fleet so
  // shell-level tests stay quiet. Tests that need rows register their own
  // handler via server.use(...), which takes precedence.
  http.get('*/v1/pipelines', () => HttpResponse.json({ items: [], limit: 100, offset: 0 })),
  // The Home dashboard (/, D7) additionally reads drafts + usage analytics on
  // mount; default to empty so tests that merely pass through '/' stay quiet.
  http.get('*/v1/drafts', () => HttpResponse.json([])),
  http.get('*/v1/analytics/usage', () => HttpResponse.json(EMPTY_USAGE)),
]

export const server = setupServer(...handlers)
