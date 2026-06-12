/**
 * msw fixtures + handlers for the admin screens (mirrors
 * environments/environmentsTestHandlers.ts). Handlers capture request
 * payloads so tests can assert exact wire shapes.
 */
import { http, HttpResponse } from 'msw'

import type {
  Connection,
  ConnectionCreate,
  ConnectionUpdate,
  HostMappingIn,
  HostMappingOut,
  ProbeResult,
} from '@/api/hooks/useConnections'
import type { Consumer, ConsumerCreated, ConsumerUpdateRequest } from '@/api/hooks/useConsumers'

const NOW = '2026-06-12T12:00:00Z'

export const CONN_JIRA: Connection = {
  id: 'conn-jira',
  kind: 'work_tracking',
  provider: 'jira',
  name: 'jira-prod',
  project_id: 'proj-alpha',
  base_url: 'https://jira.example.com',
  options: { project_key: 'ALPHA' },
  secret_ref: 'env:JIRA_API_TOKEN',
  enabled: true,
  created_at: NOW,
  updated_at: NOW,
}

export const CONN_ELK: Connection = {
  id: 'conn-elk',
  kind: 'log_search',
  provider: 'elasticsearch',
  name: 'elk-global',
  project_id: null,
  base_url: 'https://elk.example.com:9200',
  options: {},
  secret_ref: null,
  enabled: false,
  created_at: NOW,
  updated_at: NOW,
}

export const CONN_ENGINE: Connection = {
  id: 'conn-engine',
  kind: 'execution_engine',
  provider: 'apex_load',
  name: 'apex-load-default',
  project_id: 'proj-alpha',
  base_url: null,
  options: { pool: 'default' },
  secret_ref: null,
  enabled: true,
  created_at: NOW,
  updated_at: NOW,
}

export const CONNECTIONS_FIXTURE = [CONN_JIRA, CONN_ELK, CONN_ENGINE]

export const MAPPINGS_FIXTURE: HostMappingOut[] = [
  { id: 'map-1', pattern: '*.internal.example.com', target: 'proxy.example.com', enabled: true },
]

export const CONSUMER_OPS: Consumer = {
  id: 'cons-ops',
  name: 'Dash Ops',
  consumer_type: 'dashboard',
  role: 'admin',
  enabled: true,
  scopes: [{ project_id: 'proj-alpha', app_id: null }],
  created_at: NOW,
  last_used_at: NOW,
  key_fingerprint: 'a1b2c3d4',
}

export const CONSUMER_CI: Consumer = {
  id: 'cons-ci',
  name: 'ci-bot',
  consumer_type: 'headless',
  role: 'operator',
  enabled: true,
  scopes: [
    { project_id: 'demo', app_id: 'app1' },
    { project_id: 'proj-alpha', app_id: null },
    { project_id: 'proj-beta', app_id: null },
  ],
  created_at: NOW,
  last_used_at: null,
  key_fingerprint: 'deadbeef',
}

export const CONSUMERS_FIXTURE = [CONSUMER_OPS, CONSUMER_CI]

/** Read-path handlers for the connections list + detail (+ host mappings). */
export function connectionsReadHandlers(
  connections: Connection[] = CONNECTIONS_FIXTURE,
  mappings: HostMappingOut[] = MAPPINGS_FIXTURE,
) {
  return [
    http.get('*/v1/admin/connections', () => HttpResponse.json(connections)),
    http.get('*/v1/admin/connections/:id', ({ params }) => {
      const found = connections.find((candidate) => candidate.id === params.id)
      return found
        ? HttpResponse.json(found)
        : HttpResponse.json({ detail: `connection ${String(params.id)} not found` }, { status: 404 })
    }),
    http.get('*/v1/admin/connections/:id/host-mappings', () => HttpResponse.json(mappings)),
  ]
}

/**
 * POST create — answers the router's 422 (problem detail lists registered
 * providers) for unknown providers, 201 with the echoed record otherwise.
 */
export function createConnectionHandler(options: { registered?: string[]; id?: string } = {}) {
  const registered = options.registered ?? ['jira', 'azure_devops']
  const captured: ConnectionCreate[] = []
  const handler = http.post('*/v1/admin/connections', async ({ request }) => {
    const body = (await request.json()) as ConnectionCreate
    captured.push(body)
    if (!registered.includes(body.provider)) {
      return HttpResponse.json(
        {
          detail:
            `unknown provider '${body.provider}' for kind '${body.kind}'; ` +
            `registered providers: ${registered.join(', ')}`,
        },
        { status: 422 },
      )
    }
    const created: Connection = {
      id: options.id ?? 'conn-new',
      kind: body.kind,
      provider: body.provider,
      name: body.name,
      project_id: body.project_id ?? null,
      base_url: body.base_url ?? null,
      options: body.options ?? {},
      secret_ref: body.secret_ref ?? null,
      enabled: true,
      created_at: NOW,
      updated_at: NOW,
    }
    return HttpResponse.json(created, { status: 201 })
  })
  return { handler, captured }
}

/** PATCH update — captures bodies and answers with the merged record. */
export function updateConnectionHandler(base: Connection) {
  const captured: ConnectionUpdate[] = []
  const handler = http.patch('*/v1/admin/connections/:id', async ({ request }) => {
    const body = (await request.json()) as ConnectionUpdate
    captured.push(body)
    return HttpResponse.json({ ...base, ...body, updated_at: '2026-06-12T13:00:00Z' })
  })
  return { handler, captured }
}

/** POST enable/disable — captures which endpoint fired. */
export function toggleConnectionHandlers(base: Connection) {
  const calls: Array<'enable' | 'disable'> = []
  const handlers = [
    http.post('*/v1/admin/connections/:id/enable', () => {
      calls.push('enable')
      return HttpResponse.json({ ...base, enabled: true })
    }),
    http.post('*/v1/admin/connections/:id/disable', () => {
      calls.push('disable')
      return HttpResponse.json({ ...base, enabled: false })
    }),
  ]
  return { handlers, calls }
}

/** DELETE — captures ids and answers 204. */
export function deleteConnectionHandler() {
  const captured: string[] = []
  const handler = http.delete('*/v1/admin/connections/:id', ({ params }) => {
    captured.push(String(params.id))
    return new HttpResponse(null, { status: 204 })
  })
  return { handler, captured }
}

/** POST test — ALWAYS 200; failures come back inline as ok=false. */
export function probeHandler(result: ProbeResult) {
  let calls = 0
  const handler = http.post('*/v1/admin/connections/:id/test', () => {
    calls += 1
    return HttpResponse.json(result)
  })
  return { handler, callCount: () => calls }
}

/** PUT host-mappings — captures the FULL replacement list. */
export function putHostMappingsHandler() {
  const captured: HostMappingIn[][] = []
  const handler = http.put('*/v1/admin/connections/:id/host-mappings', async ({ request }) => {
    const body = (await request.json()) as HostMappingIn[]
    captured.push(body)
    const saved: HostMappingOut[] = body.map((mapping, index) => ({
      id: `map-saved-${index}`,
      pattern: mapping.pattern,
      target: mapping.target,
      enabled: mapping.enabled ?? true,
    }))
    return HttpResponse.json(saved)
  })
  return { handler, captured }
}

/** Read-path handlers for the consumers list + detail. */
export function consumersReadHandlers(consumers: Consumer[] = CONSUMERS_FIXTURE) {
  return [
    http.get('*/v1/admin/consumers', () => HttpResponse.json(consumers)),
    http.get('*/v1/admin/consumers/:id', ({ params }) => {
      const found = consumers.find((candidate) => candidate.id === params.id)
      return found
        ? HttpResponse.json(found)
        : HttpResponse.json({ detail: `Consumer '${String(params.id)}' not found` }, { status: 404 })
    }),
  ]
}

/** POST create — answers 201 with the one-time api_key payload. */
export function createConsumerHandler(apiKey = 'apex_key_only_shown_once_123') {
  const captured: unknown[] = []
  const handler = http.post('*/v1/admin/consumers', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    captured.push(body)
    const created: ConsumerCreated = {
      id: 'cons-new',
      name: String(body.name),
      consumer_type: 'headless',
      role: 'viewer',
      enabled: true,
      scopes: [],
      created_at: NOW,
      last_used_at: null,
      key_fingerprint: 'feedf00d',
      api_key: apiKey,
    }
    return HttpResponse.json(created, { status: 201 })
  })
  return { handler, captured }
}

/** POST rotate — answers with a fresh one-time api_key for the consumer. */
export function rotateConsumerHandler(base: Consumer, apiKey = 'apex_key_rotated_456') {
  let calls = 0
  const handler = http.post('*/v1/admin/consumers/:id/rotate', () => {
    calls += 1
    const rotated: ConsumerCreated = { ...base, key_fingerprint: 'r0t4t3d0', api_key: apiKey }
    return HttpResponse.json(rotated)
  })
  return { handler, callCount: () => calls }
}

/** PATCH update — captures bodies and answers with the merged record. */
export function updateConsumerHandler(base: Consumer) {
  const captured: ConsumerUpdateRequest[] = []
  const handler = http.patch('*/v1/admin/consumers/:id', async ({ request }) => {
    const body = (await request.json()) as ConsumerUpdateRequest
    captured.push(body)
    return HttpResponse.json({
      ...base,
      role: body.role ?? base.role,
      enabled: body.enabled ?? base.enabled,
      scopes: body.scopes ?? base.scopes,
    })
  })
  return { handler, captured }
}

/**
 * DELETE — answers the router's 409 self-delete problem for `selfId`,
 * 204 otherwise.
 */
export function deleteConsumerHandler(selfId?: string) {
  const captured: string[] = []
  const handler = http.delete('*/v1/admin/consumers/:id', ({ params }) => {
    const id = String(params.id)
    if (selfId && id === selfId) {
      return HttpResponse.json({ detail: 'A consumer cannot delete itself' }, { status: 409 })
    }
    captured.push(id)
    return new HttpResponse(null, { status: 204 })
  })
  return { handler, captured }
}
