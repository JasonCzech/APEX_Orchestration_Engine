import { expect } from 'vitest'
import { PHASE_NAMES } from '@apex/pipeline-events'
/**
 * Shared msw handlers + render helper for the wizard tests. Default handlers
 * cover every /v1 surface the wizard touches (the global msw server runs with
 * onUnhandledRequest:'error'); individual tests override via server.use(...).
 *
 * NOTE: the LangGraph SDK boundary (assistants/threads/runs) is NOT handled
 * here — test files that mount the Config/Review steps must vi.mock
 * '@/api/langgraphClient' themselves (vi.mock is per-module and hoisted).
 */
import { fireEvent, render, waitFor, screen as testingScreen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { createMemoryRouter, RouterProvider } from 'react-router'

import { QueryClientProvider } from '@tanstack/react-query'

import type { components } from '@apex/api-client'

import { AuthProvider, type AuthState } from '@/auth/AuthProvider'
import { NewRunWizardPage } from '@/features/new-test/NewRunWizard'
import { createTestQueryClient } from '@/test/render'
import { server } from '@/test/server'

type DraftRead = components['schemas']['DraftRead']

export const DEFAULT_PROMPT_ROWS = [
  { id: 'system-default', namespace: 'system', key: 'default', phase: null, is_default: true, is_shared: false, selection_enabled: true, active_version: { id: 'system-default-v1', version: 1 } },
  ...PHASE_NAMES.map(phase => ({ id: `phase-${phase}`, namespace: 'phase', key: `${phase}/system`, phase, is_default: true, is_shared: false, selection_enabled: true, active_version: { id: `phase-${phase}-v1`, version: 1 } })),
]

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
    target_approved: true,
    target_version: 1,
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
  updates: {
    id: string
    title: string
    project_id?: string | null
    payload: Record<string, unknown>
  }[]
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
      const body = (await request.json()) as {
        title: string
        project_id?: string | null
        payload: Record<string, unknown>
      }
      captured.updates.push({ id, ...body })
      const updated = draftRead({
        id,
        title: body.title,
        project_id: body.project_id ?? null,
        payload: body.payload,
      })
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
    http.post('*/v1/pipelines/execution-readiness', async ({ request }) => {
      const body = await request.json() as { phases: string[] }
      return HttpResponse.json({ execution_selected: body.phases.includes('execution'), configuration_valid: true, requires_application: false, detail: null })
    }),
    http.get('*/v1/pipelines/analysis/connections', () => HttpResponse.json([{ id: 'analysis-forge', name: 'Forge analysis', provider: 'perfreportforge', kind: 'analysis' }, { id: 'native-rca', name: 'Internal RCA', provider: 'apex_load', kind: 'execution_engine' }])),
    http.get('*/v1/apex-load/connections', () => HttpResponse.json({ items: [], truncated: false })),
    http.post('*/assistants/search', () => HttpResponse.json([])),
    http.get('*/v1/catalog/applications', () => HttpResponse.json(APPLICATIONS)),
    http.get('*/v1/catalog/environments', () => HttpResponse.json(ENVIRONMENTS)),
    http.get('*/v1/catalog/environments/:id', ({ params }) => {
      const environment = ENVIRONMENTS.find(item => item.id === params.id)
      return environment ? HttpResponse.json(environment) : HttpResponse.json({ title: 'Environment not found' }, { status: 404 })
    }),
    http.get('*/v1/documents', () =>
      HttpResponse.json({ items: [], limit: 50, offset: 0 }),
    ),
    http.get('*/v1/work-tracking/saved-queries', () =>
      HttpResponse.json({ items: [], limit: 50, offset: 0 }),
    ),
    http.get('*/v1/prompts', () => HttpResponse.json(DEFAULT_PROMPT_ROWS)),
    http.get('*/v1/prompts/:id/versions/:version', ({ params }) => HttpResponse.json({ id: params.version, version: 1, content: 'Default test instructions', note: null })),
  ]
}

export function installWizardHandlers(existingDrafts: DraftRead[] = []) {
  const drafts = draftHandlers(existingDrafts)
  server.use(...defaultWizardHandlers(), ...drafts.handlers)
  return drafts
}

/** Mounts the wizard on a memory router with a probe for the post-launch route. */
export function renderWizard(initialEntry = '/runs/new', authState?: AuthState) {
  const router = createMemoryRouter(
    [
      { path: '/runs/new', element: <NewRunWizardPage /> },
      { path: '/runs/:threadId', element: <div data-testid="run-page" /> },
    ],
    { initialEntries: [initialEntry] },
  )
  const queryClient = createTestQueryClient()
  const routed = <RouterProvider router={router} />
  const result = render(
    <QueryClientProvider client={queryClient}>
      {authState ? <AuthProvider staticState={authState}>{routed}</AuthProvider> : routed}
    </QueryClientProvider>,
  )
  return { ...result, router, queryClient }
}

/** Finish pending autosave before a test removes the wizard's MSW handlers. */
export async function flushAndUnmountWizard(result: ReturnType<typeof render>): Promise<void> {
  const saveState = result.queryByTestId('draft-save-state')?.textContent ?? ''
  if (/Unsaved changes|Saving|Draft save failed/.test(saveState)) {
    const save = result.queryByRole('button', { name: 'Save draft' })
    if (save) {
      fireEvent.click(save)
      await waitFor(() => {
        if (!result.queryByText('Draft saved')) throw new Error('draft flush still pending')
      })
    }
  }
  result.unmount()
}

/** Fills the three required Scope fields (makes the scope step valid). */
export async function fillScope(
  user: { type: (element: Element, text: string) => Promise<void> },
  screen: { getByLabelText: (label: string) => HTMLElement },
) {
  fireEvent.click(testingScreen.getByRole('button', { name: 'Add backend' }))
  await waitFor(() => expect(testingScreen.getByRole('option', { name: 'demo' })).toBeInTheDocument())
  fireEvent.change(screen.getByLabelText('Project'), { target: { value: 'demo' } })
  await waitFor(() => expect(testingScreen.getByRole('option', { name: /Checkout · demo/ })).toBeInTheDocument())
  fireEvent.change(screen.getByLabelText('Application'), { target: { value: APPLICATIONS[0]!.id } })
  await waitFor(() => expect(testingScreen.getByRole('option', { name: /staging/i })).toBeInTheDocument())
  fireEvent.change(screen.getByLabelText('Environment'), { target: { value: ENVIRONMENTS[0]!.id } })
  fireEvent.click(testingScreen.getByRole('radio', { name: 'Primary system under test' }))
  fireEvent.click(testingScreen.getByRole('radio', { name: 'Traffic entry' }))
  await user.type(screen.getByLabelText('Title'), 'Checkout soak')
  await user.type(screen.getByLabelText('Request'), 'Soak the checkout flow for 1h')
}
