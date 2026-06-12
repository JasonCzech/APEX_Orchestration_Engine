/**
 * msw fixtures + handlers for the environments screens (mirrors
 * runs/runsTestHandlers.ts). Handlers capture request payloads so tests can
 * assert exact wire shapes.
 */
import { http, HttpResponse } from 'msw'

import type {
  Application,
  Environment,
  EnvironmentCreate,
  EnvironmentUpdate,
} from '@/api/hooks/useEnvironments'
import type { InventoryView, SnapshotView } from '@/api/hooks/useInventory'

const NOW = '2026-06-12T12:00:00Z'

export const APP_CHECKOUT: Application = {
  id: 'app-checkout',
  name: 'Checkout',
  project_id: 'proj-alpha',
  description: 'Payment funnel services',
  archived_at: null,
  created_at: NOW,
  updated_at: NOW,
}

export const APP_SEARCH: Application = {
  id: 'app-search',
  name: 'Search',
  project_id: 'proj-alpha',
  description: null,
  archived_at: null,
  created_at: NOW,
  updated_at: NOW,
}

export const ENV_STAGING: Environment = {
  id: 'env-staging',
  application_id: 'app-checkout',
  name: 'staging',
  kind: 'k8s',
  base_url: 'https://staging.checkout.example.com',
  hosts: [{ id: 'host-1', hostname: 'stg-node-1', role: 'worker' }],
  options: { namespace: 'checkout-stg' },
  created_at: NOW,
  updated_at: NOW,
}

export const ENV_PROD: Environment = {
  id: 'env-prod',
  application_id: 'app-checkout',
  name: 'production',
  kind: 'k8s',
  base_url: 'https://checkout.example.com',
  hosts: [
    { id: 'host-2', hostname: 'prod-node-1', role: 'worker' },
    { id: 'host-3', hostname: 'prod-node-2', role: null },
  ],
  options: {},
  created_at: NOW,
  updated_at: NOW,
}

export const ENV_SEARCH_DEV: Environment = {
  id: 'env-search-dev',
  application_id: 'app-search',
  name: 'dev',
  kind: 'vm',
  base_url: null,
  hosts: [],
  options: {},
  created_at: NOW,
  updated_at: NOW,
}

export const APPS_FIXTURE = [APP_CHECKOUT, APP_SEARCH]
export const ENVS_FIXTURE = [ENV_STAGING, ENV_PROD, ENV_SEARCH_DEV]

export const SNAPSHOT_FRESH: SnapshotView = {
  scanned_at: new Date().toISOString(),
  stale: false,
  services: [
    { name: 'checkout-api', replicas: 3, image: 'registry.example.com/checkout-api:1.42.0' },
    { name: 'checkout-worker', replicas: 0, image: 'registry.example.com/checkout-worker:1.42.0' },
  ],
}

export const SNAPSHOT_STALE: SnapshotView = {
  ...SNAPSHOT_FRESH,
  scanned_at: '2026-05-01T00:00:00Z',
  stale: true,
}

export function inventoryOf(
  environmentId: string,
  snapshot: SnapshotView | null,
): InventoryView {
  return { environment_id: environmentId, snapshot }
}

/** Read-path handlers for the list + detail screens (lookup by id). */
export function catalogReadHandlers(
  apps: Application[] = APPS_FIXTURE,
  envs: Environment[] = ENVS_FIXTURE,
) {
  return [
    http.get('*/v1/catalog/applications', () => HttpResponse.json(apps)),
    http.get('*/v1/catalog/environments', () => HttpResponse.json(envs)),
    http.get('*/v1/catalog/environments/:id', ({ params }) => {
      const env = envs.find((candidate) => candidate.id === params.id)
      return env
        ? HttpResponse.json(env)
        : HttpResponse.json({ detail: `environment ${String(params.id)} not found` }, { status: 404 })
    }),
  ]
}

export function inventoryHandler(view: InventoryView) {
  return http.get('*/v1/inventory/environments/:id', () => HttpResponse.json(view))
}

/** POST create — captures bodies and answers 201 with the echoed record. */
export function createEnvironmentHandler(id = 'env-new') {
  const captured: EnvironmentCreate[] = []
  const handler = http.post('*/v1/catalog/environments', async ({ request }) => {
    const body = (await request.json()) as EnvironmentCreate
    captured.push(body)
    const created: Environment = {
      id,
      application_id: body.application_id,
      name: body.name,
      kind: body.kind ?? null,
      base_url: body.base_url ?? null,
      hosts: (body.hosts ?? []).map((host, index) => ({
        id: `host-new-${index}`,
        hostname: host.hostname,
        role: host.role ?? null,
      })),
      options: body.options ?? {},
      created_at: NOW,
      updated_at: NOW,
    }
    return HttpResponse.json(created, { status: 201 })
  })
  return { handler, captured }
}

/** PATCH update — captures bodies and answers with the merged record. */
export function updateEnvironmentHandler(base: Environment) {
  const captured: EnvironmentUpdate[] = []
  const handler = http.patch('*/v1/catalog/environments/:id', async ({ request }) => {
    const body = (await request.json()) as EnvironmentUpdate
    captured.push(body)
    const updated: Environment = {
      ...base,
      base_url: body.base_url !== undefined ? body.base_url : base.base_url,
      kind: body.kind !== undefined ? body.kind : base.kind,
      hosts:
        body.hosts != null
          ? body.hosts.map((host, index) => ({
              id: `host-upd-${index}`,
              hostname: host.hostname,
              role: host.role ?? null,
            }))
          : base.hosts,
      options: body.options != null ? body.options : base.options,
    }
    return HttpResponse.json(updated)
  })
  return { handler, captured }
}

/** DELETE — captures ids and answers 204. */
export function deleteEnvironmentHandler() {
  const captured: string[] = []
  const handler = http.delete('*/v1/catalog/environments/:id', ({ params }) => {
    captured.push(String(params.id))
    return new HttpResponse(null, { status: 204 })
  })
  return { handler, captured }
}

/**
 * POST rescan — optionally fails the first call with the router's 502 problem
 * shape (detail = adapter message), then answers with the fresh snapshot.
 */
export function rescanHandler(
  fresh: InventoryView,
  options: { failFirst?: boolean; detail?: string } = {},
) {
  let calls = 0
  const handler = http.post('*/v1/inventory/environments/:id/rescan', () => {
    calls += 1
    if (options.failFirst && calls === 1) {
      return HttpResponse.json(
        { detail: options.detail ?? 'environment rescan failed: adapter unreachable' },
        { status: 502 },
      )
    }
    return HttpResponse.json(fresh)
  })
  return { handler, callCount: () => calls }
}
