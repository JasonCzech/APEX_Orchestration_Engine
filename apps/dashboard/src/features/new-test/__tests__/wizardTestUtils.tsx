/**
 * Shared msw handlers + render helper for the wizard tests. Default handlers
 * cover every /v1 surface the wizard touches (the global msw server runs with
 * onUnhandledRequest:'error'); individual tests override via server.use(...).
 *
 * NOTE: the LangGraph SDK boundary (assistants/threads/runs) is NOT handled
 * here — test files that mount the Config/Review steps must vi.mock
 * '@/api/langgraphClient' themselves (vi.mock is per-module and hoisted).
 */
import { render } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'

import { QueryClientProvider } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { NewRunWizardPage } from '@/features/new-test/NewRunWizard'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

type DraftRead = components['schemas']['DraftRead']

export const APPLICATIONS = [
  {
    id: 'app-checkout',
    project_id: 'demo',
    name: 'Checkout',
    description: 'Payments and cart funnel',
    archived_at: null,
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
  },
]

export const ENVIRONMENTS = [
  {
    id: 'env-staging',
    application_id: 'app-checkout',
    name: 'staging',
    kind: 'staging',
    base_url: 'https://staging.checkout.example',
    hosts: [],
    options: {},
    created_at: '2026-06-01T00:00:00Z',
    updated_at: '2026-06-01T00:00:00Z',
  },
]

export function draftRead(overrides: Partial<DraftRead> = {}): DraftRead {
  return {
    id: 'draft-1',
    title: 'Untitled run',
    project_id: 'demo',
    payload: {},
    created_by: 'dash-ops',
    created_at: '2026-06-10T00:00:00Z',
    updated_at: '2026-06-10T00:00:00Z',
    ...overrides,
  }
}

export interface DraftCapture {
  creates: { title: string; project_id?: string | null; payload: Record<string, unknown> }[]
  updates: { id: string; title: string; payload: Record<string, unknown> }[]
  deletes: string[]
}

/**
 * Scriptable draft endpoints: POST mints sequential ids, PUT/DELETE capture.
 * `existing` seeds the listDrafts response (resume picker).
 */
export function draftHandlers(existing: DraftRead[] = []) {
  const captured: DraftCapture = { creates: [], updates: [], deletes: [] }
  const store = new Map(existing.map((entry) => [entry.id, entry]))
  let nextId = 1
  const handlers = [
    http.get('*/v1/drafts', () => HttpResponse.json([...store.values()])),
    http.get('*/v1/drafts/:id', ({ params }) => {
      const found = store.get(params['id'] as string)
      return found
        ? HttpResponse.json(found)
        : HttpResponse.json({ detail: 'not found' }, { status: 404 })
    }),
    http.post('*/v1/drafts', async ({ request }) => {
      const body = (await request.json()) as DraftCapture['creates'][number]
      captured.creates.push(body)
      const created = draftRead({
        id: `draft-${nextId++}`,
        title: body.title,
        project_id: body.project_id ?? null,
        payload: body.payload,
      })
      store.set(created.id, created)
      return HttpResponse.json(created, { status: 201 })
    }),
    http.put('*/v1/drafts/:id', async ({ params, request }) => {
      const id = params['id'] as string
      const body = (await request.json()) as { title: string; payload: Record<string, unknown> }
      captured.updates.push({ id, ...body })
      const updated = draftRead({ id, title: body.title, payload: body.payload })
      store.set(id, updated)
      return HttpResponse.json(updated)
    }),
    http.delete('*/v1/drafts/:id', ({ params }) => {
      captured.deletes.push(params['id'] as string)
      return new HttpResponse(null, { status: 204 })
    }),
  ]
  return { handlers, captured }
}

/** Every wizard-touched read surface, quiet by default. */
export function defaultWizardHandlers() {
  return [
    http.get('*/v1/catalog/applications', () => HttpResponse.json(APPLICATIONS)),
    http.get('*/v1/catalog/environments', () => HttpResponse.json(ENVIRONMENTS)),
    http.get('*/v1/documents', () =>
      HttpResponse.json({ items: [], limit: 50, offset: 0 }),
    ),
    http.get('*/v1/work-tracking/saved-queries', () =>
      HttpResponse.json({ items: [], limit: 50, offset: 0 }),
    ),
    http.get('*/v1/prompts', () => HttpResponse.json([])),
  ]
}

export function installWizardHandlers(existingDrafts: DraftRead[] = []) {
  const drafts = draftHandlers(existingDrafts)
  server.use(...defaultWizardHandlers(), ...drafts.handlers)
  return drafts
}

/** Mounts the wizard on a memory router with a probe for the post-launch route. */
export function renderWizard(initialEntry = '/runs/new') {
  const router = createMemoryRouter(
    [
      { path: '/runs/new', element: <NewRunWizardPage /> },
      { path: '/runs/:threadId', element: <div data-testid="run-page" /> },
    ],
    { initialEntries: [initialEntry] },
  )
  const queryClient = createTestQueryClient()
  const result = render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { ...result, router, queryClient }
}

/** Fills the three required Scope fields (makes the scope step valid). */
export async function fillScope(
  user: { type: (element: Element, text: string) => Promise<void> },
  screen: { getByLabelText: (label: string) => HTMLElement },
) {
  await user.type(screen.getByLabelText('Title'), 'Checkout soak')
  await user.type(screen.getByLabelText('Request'), 'Soak the checkout flow for 1h')
}
