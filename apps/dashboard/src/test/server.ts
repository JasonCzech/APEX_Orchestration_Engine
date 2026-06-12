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

export const handlers = [
  http.get('*/v1/system/info', () => HttpResponse.json(SYSTEM_INFO)),
  // The shell's Approvals badge (Sidebar -> useApprovalsInbox, D3) polls the
  // pipelines list on every authenticated mount; default to an empty fleet so
  // shell-level tests stay quiet. Tests that need rows register their own
  // handler via server.use(...), which takes precedence.
  http.get('*/v1/pipelines', () => HttpResponse.json({ items: [], limit: 100, offset: 0 })),
]

export const server = setupServer(...handlers)
