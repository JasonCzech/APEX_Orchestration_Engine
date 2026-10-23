import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

const applications = [{ id: 'a1', name: 'Checkout', project_ids: ['p1'] }, { id: 'a2', name: 'Search', project_ids: ['p2'] }]

// The project bar (?project=) is where a project's display name is shown and edited.
function mount(scopes: { project_id: string; app_id: string | null }[] = [], entry = '/environments?project=p1') {
  server.use(http.get('*/v1/catalog/applications', () => HttpResponse.json(applications)), http.get('*/v1/catalog/environments', () => HttpResponse.json([])))
  return renderApp({ initialEntries: [entry], authState: authenticatedState('admin', 'Admin', scopes) })
}

describe('authoritative project names', () => {
  it('disambiguates duplicate names with full IDs and saves a rename to the selected project', async () => {
    let labels = [{ project_id: 'p1', name: 'Payments' }, { project_id: 'p2', name: 'Payments' }]
    const writes: unknown[] = []
    server.use(http.get('*/v1/catalog/projects', () => HttpResponse.json(labels)), http.put('*/v1/catalog/projects', async ({ request }) => {
      const body = await request.json() as { project_id: string; name: string }
      writes.push(body); labels = labels.map(row => row.project_id === body.project_id ? body : row)
      return HttpResponse.json({ ...body, updated_at: '2026-10-17T12:00:00Z' })
    }))
    mount()
    // Duplicate display names stay distinguishable: the select shows the ID, the bar shows the copyable ID.
    expect(await screen.findByRole('option', { name: 'Payments (p2)' })).toBeInTheDocument()
    const bar = screen.getByRole('region', { name: 'Project Payments (p1)' })
    expect(within(bar).getByText('p1', { exact: true })).toHaveAttribute('title', 'p1')
    const user = userEvent.setup()
    await user.click(within(bar).getByRole('button', { name: 'Edit project name' }))
    const dialog = screen.getByRole('dialog', { name: 'Project display name' })
    await user.clear(within(dialog).getByRole('textbox', { name: 'Project name' }))
    await user.type(within(dialog).getByRole('textbox', { name: 'Project name' }), 'Payments renamed')
    await user.click(within(dialog).getByRole('button', { name: 'Save' }))
    expect(await within(bar).findByText('Payments renamed')).toBeVisible()
    expect(screen.getByRole('option', { name: 'Payments (p2)' })).toBeInTheDocument()
    expect(writes).toEqual([{ project_id: 'p1', name: 'Payments renamed' }])
  })

  it('keeps IDs and explicit unavailable labels after denied lookup and hides project edits for app admins', async () => {
    server.use(http.get('*/v1/catalog/projects', () => HttpResponse.json({ detail: 'Forbidden' }, { status: 403 })))
    mount([{ project_id: 'p1', app_id: 'a1' }])
    expect((await screen.findAllByText('Project name unavailable')).length).toBeGreaterThan(0)
    expect(screen.getByText('p1', { exact: true, selector: 'code' })).toHaveAttribute('title', 'p1')
    expect(screen.queryByRole('button', { name: 'Edit project name' })).not.toBeInTheDocument()
  })

  it('uses authorized display names in project choices while preserving the full ID as the value', async () => {
    server.use(http.get('*/v1/catalog/projects', () => HttpResponse.json([{ project_id: 'p1', name: 'Payments' }, { project_id: 'p2', name: 'Payments' }])))
    mount([{ project_id: 'p1', app_id: null }, { project_id: 'p2', app_id: 'a2' }], '/environments')
    const first = await screen.findByRole('option', { name: 'Payments (p1)' })
    expect(first).toHaveValue('p1')
    expect(screen.getByRole('option', { name: 'Payments (p2)' })).toHaveValue('p2')
    const user = userEvent.setup()
    await user.selectOptions(first.closest('select')!, 'p1')
    expect(first.closest('select')).toHaveValue('p1')
  })

  it('guards unsaved label edits through route navigation and removes metadata without deleting resources', async () => {
    let labels = [{ project_id: 'p1', name: 'Payments' }]
    const removed: string[] = []
    server.use(http.get('*/v1/catalog/projects', () => HttpResponse.json(labels)), http.delete('*/v1/catalog/projects', ({ request }) => { removed.push(new URL(request.url).searchParams.get('project')!); labels = []; return new HttpResponse(null, { status: 204 }) }))
    const { router } = mount([{ project_id: 'p1', app_id: null }])
    await screen.findAllByText('Payments')
    const user = userEvent.setup()
    await user.click(screen.getByRole('button', { name: 'Edit project name' }))
    await user.type(screen.getByRole('textbox', { name: 'Project name' }), ' draft')
    await act(async () => { void router.navigate('/prompts') })
    const guard = await screen.findByRole('dialog', { name: 'Discard project name changes' })
    await user.click(within(guard).getByRole('button', { name: 'Continue editing' }))
    expect(screen.getByRole('textbox', { name: 'Project name' })).toHaveValue('Payments draft')
    await user.click(screen.getByRole('checkbox', { name: /Remove this display name/ }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Project display name' })).not.toBeInTheDocument())
    expect(removed).toEqual(['p1'])
    expect(screen.getByRole('heading', { name: 'Checkout' })).toBeVisible()
    expect(screen.getAllByText('Project name unavailable').length).toBeGreaterThan(0)
  })
})
