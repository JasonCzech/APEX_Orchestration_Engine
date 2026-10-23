import { act, screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import type { Cycle } from '../api'
import { cycleNextAction } from '../cycleDisplay'

const progress = { status: 'stale', approved: 1, total: 6, deliverables: [
  { kind: 'scope', version_id: 'scope', status: 'approved' },
  { kind: 'test_plan', version_id: 'plan', status: 'stale' },
  { kind: 'readiness', version_id: 'readiness', status: 'in_review' },
  { kind: 'script', version_id: 'script', status: 'archived' },
  { kind: 'report', version_id: null, status: 'not_started' },
  { kind: 'postmortem', version_id: null, status: 'not_started' },
] }
const cycle = { id: 'c1', name: 'Checkout cycle', project_id: 'p1', app_id: 'a1', environment_id: 'e1', owner: 'author', owner_name: 'Morgan', mode: 'simulation', revision: 1, independent_review: true, created_at: '2026-10-17T12:00:00Z', updated_at: '2026-10-17T12:00:00Z', progress }
function mount(path = '/cycles') {
  const state = authenticatedState('viewer', 'Viewer', [{ project_id: 'p1', app_id: null }])
  if (state.status === 'authenticated') state.systemInfo.features = { cycles: true }
  server.use(http.get('*/v1/cycles/c1/executions', () => HttpResponse.json({ preparations: [], connections: [], linked_runs: [], recent_runs: [], revision: 1 })), http.get('*/v1/catalog/applications', () => HttpResponse.json([{ id: 'a1', project_ids: ['p1'], name: 'Checkout' }])), http.get('*/v1/catalog/environments', () => HttpResponse.json([{ id: 'e1', application_id: 'a1', name: 'Staging' }])), http.get('*/v1/catalog/projects', () => HttpResponse.json([{ project_id: 'p1', name: 'Payments' }])))
  return renderApp({ initialEntries: [path], authState: state })
}

describe('cycle identity and persisted list progress', () => {
  it('shows summary evidence without loading detail or directory per card and keeps full IDs for duplicate names', async () => {
    const details: string[] = []
    server.use(http.get('*/v1/cycles', () => HttpResponse.json([cycle, { ...cycle, id: 'c2', name: 'Other checkout', owner: 'other-author', owner_name: 'Morgan', progress: null }])), http.get('*/v1/cycles/:id', ({ params }) => { details.push(String(params.id)); return HttpResponse.json({}) }))
    mount()
    const first = (await screen.findByRole('link', { name: 'Checkout cycle' })).closest('article')!
    expect(await within(first).findByText('Payments')).toBeVisible()
    expect(within(first).getByText('author', { selector: 'code' })).toHaveAttribute('title', 'author')
    expect(within(first).getByText('Morgan')).toBeVisible()
    const evidence = within(first).getByRole('region', { name: 'Deliverable progress' })
    expect(evidence).toHaveTextContent('Approval stale · 1/6 deliverables approved')
    expect(evidence).toHaveTextContent('Script: Archived')
    expect(evidence).toHaveTextContent('Test plan: Approval stale')
    const second = screen.getByRole('link', { name: 'Other checkout' }).closest('article')!
    expect(within(second).getByText('other-author', { selector: 'code' })).toBeInTheDocument()
    expect(within(second).getByText('Deliverable progress unavailable')).toBeVisible()
    expect(details).toEqual([])
    expect(screen.queryByRole('button', { name: 'Edit project name' })).not.toBeInTheDocument()
  })

  it('refreshes referenced actor names and removes stale names after a denied lookup while retaining IDs', async () => {
    let denied = false
    let name = 'Original author'
    server.use(http.get('*/v1/cycles/c1', () => HttpResponse.json({ ...cycle, owner_name: null, fields: {}, followups: [], blockers: [], versions: [] })), http.get('*/v1/cycles/c1/actors', () => denied ? HttpResponse.json({ detail: 'Forbidden' }, { status: 403 }) : HttpResponse.json({ names: { author: name }, truncated: false })))
    const { queryClient } = mount('/cycles/c1')
    expect(await screen.findByText('Original author')).toBeVisible()
    name = 'Renamed author'
    await act(() => queryClient.invalidateQueries({ queryKey: ['cycles', 'c1', 'actors'] }))
    expect(await screen.findByText('Renamed author')).toBeVisible()
    expect(screen.queryByText('Original author')).not.toBeInTheDocument()
    denied = true
    await act(() => queryClient.invalidateQueries({ queryKey: ['cycles', 'c1', 'actors'] }))
    expect(await screen.findByText('Name unavailable')).toBeVisible()
    expect(screen.queryByText('Renamed author')).not.toBeInTheDocument()
    expect(screen.getByText('author', { selector: 'code' })).toBeVisible()
  })

  it('does not resurrect an earlier approved version after archiving the latest', () => {
    const earlier = { id: 'old', kind: 'scope', status: 'approved', approval_current: true, archived: false } as Cycle['versions'][number]
    const current = { ...earlier, id: 'new', archived: true }
    expect(cycleNextAction({ versions: [earlier, current], blockers: [] } as unknown as Cycle)).toBe('Create a scope draft')
  })
})
