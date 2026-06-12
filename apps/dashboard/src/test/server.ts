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
]

export const server = setupServer(...handlers)
