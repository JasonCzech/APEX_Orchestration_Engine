/**
 * msw fixtures + handlers for the work-items screens (mirrors
 * environmentsTestHandlers.ts). Handlers capture request payloads so tests
 * can assert exact wire shapes.
 */
import { http, HttpResponse } from 'msw'

import type {
  Enrichment,
  SavedQuery,
  SavedQueryCreate,
  SavedQueryUpdate,
  ProviderQuery,
  ResolvedWorkItem,
  TranslatedQuery,
  WorkItem,
  WorkItemDraft,
  WorkItemPage,
  WorkTrackingBinding,
} from '@/api/hooks/useWorkTracking'

const NOW = '2026-06-12T12:00:00Z'

export const ITEM_PAYMENT: WorkItem = {
  key: 'PHX-101',
  title: 'Checkout retries drop payments',
  kind: 'story',
  status: 'open',
  description: 'Retries on the payment gateway drop the cart.\n\nObserved on staging since 1.42.',
  url: 'https://tracker.example.com/browse/PHX-101',
}

export const ITEM_BUG: WorkItem = {
  key: 'PHX-102',
  title: 'Gateway 502s under load',
  kind: 'bug',
  status: 'in_progress',
  description: '',
  url: null,
}

export const TRANSLATED: TranslatedQuery = {
  provider: 'jira',
  query: 'project = PHX AND status = "Open"',
  confidence: 0.82,
  connection_id: 'conn-jira',
}

export const SAVED_OPEN: SavedQuery = {
  id: 'sq-open',
  name: 'Open payment stories',
  provider: 'jira',
  query: 'project = PHX AND status = "Open" ORDER BY created DESC',
  description: 'Sprint triage pick list',
  project_id: 'proj-alpha',
  connection_id: 'conn-jira',
  created_by: 'ops',
  created_at: NOW,
  updated_at: NOW,
}

export const SAVED_BUGS: SavedQuery = {
  id: 'sq-bugs',
  name: 'Load bugs',
  provider: 'ado',
  query: "SELECT [System.Id] FROM WorkItems WHERE [System.WorkItemType] = 'Bug'",
  description: null,
  project_id: null,
  connection_id: null,
  created_by: null,
  created_at: NOW,
  updated_at: NOW,
}

export const SAVED_OPEN_LEGACY: SavedQuery = {
  ...SAVED_OPEN,
  id: 'sq-open-legacy',
  connection_id: null,
}

interface ExecuteBody {
  query: ProviderQuery
  connection_id?: string
  limit: number
  offset: number
}

export function resolvedWorkItemPage(
  items: WorkItem[],
  total = items.length,
  provider = 'jira',
  connectionId = provider === 'ado' ? 'conn-ado' : 'conn-jira',
): WorkItemPage {
  return {
    items,
    total,
    connection_id: connectionId,
    provider,
  }
}

export function workTrackingBindingHandler(
  result: WorkTrackingBinding = {
    connection_id: 'conn-jira',
    provider: 'jira',
  },
) {
  const projects: Array<string | null> = []
  const connectionIds: Array<string | null> = []
  const handler = http.get('*/v1/work-tracking/binding', ({ request }) => {
    const params = new URL(request.url).searchParams
    projects.push(params.get('project'))
    connectionIds.push(params.get('connection_id'))
    return HttpResponse.json(result)
  })
  return { handler, projects, connectionIds }
}

/** POST query/translate — captures bodies, answers a fixed translation. */
export function translateHandler(result: TranslatedQuery = TRANSLATED) {
  const captured: Array<{ text: string; connection_id?: string }> = []
  const projects: Array<string | null> = []
  const handler = http.post('*/v1/work-tracking/query/translate', async ({ request }) => {
    projects.push(new URL(request.url).searchParams.get('project'))
    captured.push((await request.json()) as { text: string; connection_id?: string })
    return HttpResponse.json(result)
  })
  return { handler, captured, projects }
}

/** POST query/execute — captures bodies, answers a fixed page. */
export function executeHandler(items: WorkItem[] = [ITEM_PAYMENT, ITEM_BUG], total = items.length) {
  const captured: ExecuteBody[] = []
  const projects: Array<string | null> = []
  const handler = http.post('*/v1/work-tracking/query/execute', async ({ request }) => {
    projects.push(new URL(request.url).searchParams.get('project'))
    const body = (await request.json()) as ExecuteBody
    captured.push(body)
    const provider = body.query.provider
    return HttpResponse.json(
      resolvedWorkItemPage(
        items,
        total,
        provider,
        body.connection_id ?? (provider === 'ado' ? 'conn-ado' : 'conn-jira'),
      ),
    )
  })
  return { handler, captured, projects }
}

export function savedQueriesHandler(items: SavedQuery[] = [SAVED_OPEN, SAVED_BUGS]) {
  return http.get('*/v1/work-tracking/saved-queries', () =>
    HttpResponse.json({ items, limit: 50, offset: 0 }),
  )
}

/** POST saved-queries — captures bodies, answers 201 with the echoed record. */
export function createSavedQueryHandler(id = 'sq-new') {
  const captured: SavedQueryCreate[] = []
  const handler = http.post('*/v1/work-tracking/saved-queries', async ({ request }) => {
    const body = (await request.json()) as SavedQueryCreate
    captured.push(body)
    const created: SavedQuery = {
      id,
      name: body.name,
      provider: body.provider,
      query: body.query,
      description: body.description ?? null,
      project_id: body.project_id ?? null,
      connection_id: body.connection_id ?? null,
      created_by: 'ops',
      created_at: NOW,
      updated_at: NOW,
    }
    return HttpResponse.json(created, { status: 201 })
  })
  return { handler, captured }
}

/** PATCH saved-queries/{id} — captures bodies, answers the merged record. */
export function updateSavedQueryHandler(base: SavedQuery) {
  const captured: SavedQueryUpdate[] = []
  const handler = http.patch('*/v1/work-tracking/saved-queries/:id', async ({ request }) => {
    const body = (await request.json()) as SavedQueryUpdate
    captured.push(body)
    return HttpResponse.json({
      ...base,
      name: body.name ?? base.name,
      provider: body.provider ?? base.provider,
      query: body.query ?? base.query,
      description: body.description !== undefined ? body.description : base.description,
      connection_id:
        body.connection_id !== undefined ? body.connection_id : base.connection_id,
    })
  })
  return { handler, captured }
}

/** DELETE saved-queries/{id} — captures ids, answers 204. */
export function deleteSavedQueryHandler() {
  const captured: string[] = []
  const handler = http.delete('*/v1/work-tracking/saved-queries/:id', ({ params }) => {
    captured.push(String(params.id))
    return new HttpResponse(null, { status: 204 })
  })
  return { handler, captured }
}

/** GET items/{key} — lookup over the fixture set; misses answer the 404 problem shape. */
export function getItemHandler(items: WorkItem[] = [ITEM_PAYMENT, ITEM_BUG]) {
  return http.get('*/v1/work-tracking/items/:key', ({ params, request }) => {
    const item = items.find((candidate) => candidate.key === params.key)
    const connectionId =
      new URL(request.url).searchParams.get('connection_id') ?? 'conn-jira'
    return item
      ? HttpResponse.json({
          ...item,
          connection_id: connectionId,
          provider: connectionId === 'conn-ado' ? 'ado' : 'jira',
        } satisfies ResolvedWorkItem)
      : HttpResponse.json(
          { detail: `work item ${String(params.key)} not found` },
          { status: 404 },
        )
  })
}

/** POST items — captures drafts, answers 201 with a keyed item. */
export function createItemHandler(key = 'PHX-300') {
  const captured: WorkItemDraft[] = []
  const idempotencyKeys: string[] = []
  const connectionIds: string[] = []
  const handler = http.post('*/v1/work-tracking/items', async ({ request }) => {
    idempotencyKeys.push(request.headers.get('Idempotency-Key') ?? '')
    const connectionId =
      new URL(request.url).searchParams.get('connection_id') ?? 'conn-jira'
    connectionIds.push(connectionId)
    const body = (await request.json()) as WorkItemDraft
    captured.push(body)
    const created: WorkItem = {
      key,
      title: body.title,
      kind: body.kind,
      status: 'open',
      description: body.description,
      url: null,
    }
    return HttpResponse.json({
      ...created,
      connection_id: connectionId,
      provider: connectionId === 'conn-ado' ? 'ado' : 'jira',
    } satisfies ResolvedWorkItem, { status: 201 })
  })
  return { handler, captured, idempotencyKeys, connectionIds }
}

/** POST items/{key}/enrich — captures bodies, answers the provided refreshed item. */
export function enrichHandler(result: WorkItem) {
  const captured: Enrichment[] = []
  const idempotencyKeys: string[] = []
  const connectionIds: string[] = []
  const handler = http.post('*/v1/work-tracking/items/:key/enrich', async ({ request }) => {
    idempotencyKeys.push(request.headers.get('Idempotency-Key') ?? '')
    const connectionId =
      new URL(request.url).searchParams.get('connection_id') ?? 'conn-jira'
    connectionIds.push(connectionId)
    captured.push((await request.json()) as Enrichment)
    return HttpResponse.json({
      ...result,
      connection_id: connectionId,
      provider: connectionId === 'conn-ado' ? 'ado' : 'jira',
    } satisfies ResolvedWorkItem)
  })
  return { handler, captured, idempotencyKeys, connectionIds }
}
