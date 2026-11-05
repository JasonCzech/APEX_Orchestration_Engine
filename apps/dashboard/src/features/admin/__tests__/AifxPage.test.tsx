import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import type { AifxApplicationSource, AifxOrganization, AifxPreview } from '@/api/hooks/useAifx'
import type { AuthState } from '@/auth/AuthProvider'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { connectionsReadHandlers, CONN_JIRA } from './adminTestHandlers'

const AT = '2026-11-04T12:00:00Z'
const CONN_GHE = {
  ...CONN_JIRA,
  id: 'conn-ghe',
  name: 'ghe-aifx',
  kind: 'source_control' as const,
  provider: 'github_enterprise',
  project_id: null,
}
const ORG: AifxOrganization = {
  id: 'org-1',
  name: 'Consumer & Mobility',
  connection_id: 'conn-ghe',
  ghe_owner: 'aifx',
  global_rules_repo: 'global-rules',
  team_documents_repo_pattern: '{apm}-team-documents',
  acb_repo_pattern: '{apm}-acb',
  default_ref: 'main',
  enabled: true,
  created_at: AT,
  updated_at: AT,
}
const ACC_SOURCE: AifxApplicationSource = {
  application_id: 'app-acc',
  application_name: 'ACC',
  organization_id: 'org-1',
  apm_id: 'APM0012',
  team_documents_repo: 'apm0012-team-documents',
  acb_repo: 'apm0012-acb',
  ref: null,
  enabled: true,
  updated_at: AT,
}
const APPLICATIONS = [
  { id: 'app-acc', name: 'ACC', description: null, project_ids: ['proj-alpha'], archived_at: null, created_at: AT, updated_at: AT },
  { id: 'app-opus', name: 'OPUS', description: null, project_ids: ['proj-alpha'], archived_at: null, created_at: AT, updated_at: AT },
]
const PREVIEW: AifxPreview = {
  ok: false,
  total_chars: 120,
  sources: [
    {
      role: 'global-rules',
      repo: 'aifx/global-rules',
      ref: 'main',
      ok: true,
      commit_sha: '0123456789abcdef0123456789abcdef01234567',
      web_url: 'https://ghe.example.test/aifx/global-rules/tree/0123456789abcdef0123456789abcdef01234567',
      application_id: null,
      entries: [{ id: 'evidence-first', type: 'rule', title: 'Evidence first', path: 'rules/e.md', phases: 'all', chars: 120 }],
    },
    { role: 'application-context', repo: 'aifx/apm0013-acb', ref: 'main', ok: false, commit_sha: null, web_url: null, application_id: 'app-opus', entries: [] },
  ],
  issues: [{ repo: 'aifx/apm0013-acb', path: 'memories/ops.md', lines: [3], message: 'text looks like a credential; remove or rephrase it' }],
}

const OPUS_SOURCE: AifxApplicationSource = {
  ...ACC_SOURCE,
  application_id: 'app-opus',
  application_name: 'OPUS',
  apm_id: 'APM0013',
  team_documents_repo: 'apm0013-team-documents',
  acb_repo: 'apm0013-acb',
}

function handlers(sources: AifxApplicationSource[] = [ACC_SOURCE], organizations: AifxOrganization[] = [ORG]) {
  const captured: { put: unknown[]; preview: unknown[]; deleted: string[] } = { put: [], preview: [], deleted: [] }
  let current = sources
  return {
    captured,
    list: [
      ...connectionsReadHandlers([CONN_JIRA, CONN_GHE]),
      http.get('*/v1/catalog/applications', () => HttpResponse.json(APPLICATIONS)),
      http.get('*/v1/admin/aifx/organizations', () => HttpResponse.json(organizations)),
      http.get('*/v1/admin/aifx/applications', () => HttpResponse.json(current)),
      http.delete('*/v1/admin/aifx/applications/:id', ({ params }) => {
        captured.deleted.push(String(params.id))
        current = current.filter((source) => source.application_id !== String(params.id))
        return new HttpResponse(null, { status: 204 })
      }),
      http.get('*/v1/admin/aifx/applications/:id/suggest', ({ request }) => {
        const apm = new URL(request.url).searchParams.get('apm_id')!.toLowerCase()
        return HttpResponse.json({ organization_id: 'org-1', apm_id: apm, team_documents_repo: `${apm}-team-documents`, acb_repo: `${apm}-acb`, ref: 'main' })
      }),
      http.put('*/v1/admin/aifx/applications/:id', async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>
        captured.put.push(body)
        return HttpResponse.json({ ...ACC_SOURCE, ...body, application_id: String(params.id), application_name: 'OPUS' })
      }),
      http.post('*/v1/admin/aifx/applications/:id/preview', async ({ request }) => {
        const text = await request.text()
        captured.preview.push(text ? JSON.parse(text) : null)
        return HttpResponse.json(PREVIEW)
      }),
    ],
  }
}

function aifxState(scopes: { project_id: string; app_id: string | null }[], aifx: boolean): AuthState {
  const state = authenticatedState('admin', 'Platform Admin', scopes)
  if (state.status !== 'authenticated') throw new Error('expected an authenticated state')
  return { ...state, systemInfo: { ...state.systemInfo, features: { ...state.systemInfo.features, aifx } } }
}

function renderPage(scopes = [] as { project_id: string; app_id: string | null }[], aifx = true) {
  return renderApp({ initialEntries: ['/admin/aifx'], authState: aifxState(scopes, aifx) })
}

describe('AifxPage', () => {
  it('lists onboarded organizations and applications', async () => {
    server.use(...handlers().list)
    renderPage()
    const orgs = await screen.findByRole('table', { name: 'Organizations' })
    expect(within(orgs).getByText('Consumer & Mobility')).toBeInTheDocument()
    expect(await within(orgs).findByRole('link', { name: 'ghe-aifx' })).toHaveAttribute('href', '/admin/connections/conn-ghe')
    const apps = screen.getByRole('table', { name: 'Onboarded applications' })
    const row = within(apps).getByTestId('aifx-app-app-acc')
    expect(within(row).getByText('APM0012')).toBeInTheDocument()
    expect(within(row).getByText('apm0012-acb')).toBeInTheDocument()
  })

  it('suggests repositories from conventions, validates the draft, then saves it', async () => {
    const { list, captured } = handlers()
    server.use(...list)
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Onboard application' }))
    const form = screen.getByRole('form', { name: 'Onboard application' })
    await user.selectOptions(within(form).getByLabelText('Application'), 'app-opus')
    await user.type(within(form).getByLabelText('APM ID'), 'APM0013')
    await user.click(within(form).getByRole('button', { name: 'Suggest repositories' }))
    await waitFor(() => expect(within(form).getByLabelText('ACB repository')).toHaveValue('apm0013-acb'))
    expect(within(form).getByLabelText('Team documents repository')).toHaveValue('apm0013-team-documents')

    await user.click(within(form).getByRole('button', { name: 'Validate' }))
    const preview = await within(form).findByTestId('aifx-preview')
    expect(within(preview).getByRole('status')).toHaveTextContent('1 problem to fix')
    expect(within(preview).getByText(/aifx\/apm0013-acb:memories\/ops\.md:3/)).toBeInTheDocument()
    expect(within(preview).getByRole('link', { name: '0123456' })).toHaveAttribute('target', '_blank')
    expect(captured.preview[0]).toMatchObject({ organization_id: 'org-1', apm_id: 'APM0013', acb_repo: 'apm0013-acb' })

    await user.click(within(form).getByRole('button', { name: 'Save mapping' }))
    await waitFor(() => expect(captured.put).toHaveLength(1))
    expect(captured.put[0]).toMatchObject({
      organization_id: 'org-1',
      apm_id: 'APM0013',
      team_documents_repo: 'apm0013-team-documents',
      acb_repo: 'apm0013-acb',
      ref: null,
    })
  })

  it('validates a saved mapping without sending a draft', async () => {
    const { list, captured } = handlers()
    server.use(...list)
    const user = userEvent.setup()
    renderPage()
    const row = await screen.findByTestId('aifx-app-app-acc')
    await user.click(within(row).getByRole('button', { name: 'Validate' }))
    await screen.findByTestId('aifx-preview')
    expect(captured.preview).toEqual([null])
  })

  it('offers organization onboarding only to platform admins', async () => {
    server.use(...handlers().list)
    renderPage([{ project_id: 'proj-alpha', app_id: null }])
    await screen.findByRole('table', { name: 'Organizations' })
    expect(screen.queryByRole('button', { name: 'Onboard organization' })).not.toBeInTheDocument()
  })

  it('onboards an organization against a global source-control connection', async () => {
    const created: unknown[] = []
    server.use(
      ...handlers([]).list,
      http.get('*/v1/admin/aifx/organizations', () => HttpResponse.json([])),
      http.post('*/v1/admin/aifx/organizations', async ({ request }) => {
        const body = await request.json()
        created.push(body)
        return HttpResponse.json({ ...ORG, ...(body as object) }, { status: 201 })
      }),
    )
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Onboard organization' }))
    const form = screen.getByRole('form', { name: 'Onboard organization' })
    const connection = within(form).getByLabelText('Connection')
    expect(within(connection).queryByRole('option', { name: /jira-prod/ })).not.toBeInTheDocument()
    await user.type(within(form).getByLabelText('Name'), 'Consumer & Mobility')
    await user.selectOptions(connection, 'conn-ghe')
    await user.type(within(form).getByLabelText('GitHub owner'), 'aifx')
    await user.click(within(form).getByRole('button', { name: 'Onboard organization' }))
    await waitFor(() => expect(created).toHaveLength(1))
    expect(created[0]).toMatchObject({
      name: 'Consumer & Mobility',
      connection_id: 'conn-ghe',
      ghe_owner: 'aifx',
      global_rules_repo: 'global-rules',
      team_documents_repo_pattern: '{apm}-team-documents',
      acb_repo_pattern: '{apm}-acb',
      default_ref: 'main',
    })
  })

  it('marks a draft validation stale once the form changes, until validated again', async () => {
    const { list, captured } = handlers()
    server.use(...list)
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Onboard application' }))
    const form = screen.getByRole('form', { name: 'Onboard application' })
    await user.selectOptions(within(form).getByLabelText('Application'), 'app-opus')
    await user.type(within(form).getByLabelText('APM ID'), 'APM0013')
    await user.click(within(form).getByRole('button', { name: 'Validate' }))
    await within(form).findByTestId('aifx-preview')
    expect(within(form).queryByTestId('aifx-preview-stale')).not.toBeInTheDocument()

    await user.type(within(form).getByLabelText('Ref'), 'v2')
    expect(within(form).queryByTestId('aifx-preview')).not.toBeInTheDocument()
    expect(within(form).getByTestId('aifx-preview-stale')).toHaveTextContent('The mapping changed since it was validated')

    await user.click(within(form).getByRole('button', { name: 'Validate' }))
    await within(form).findByTestId('aifx-preview')
    expect(within(form).queryByTestId('aifx-preview-stale')).not.toBeInTheDocument()
    expect(captured.preview).toHaveLength(2)
    expect(captured.preview[1]).toMatchObject({ apm_id: 'APM0013', ref: 'v2' })
  })

  it('ties a saved-mapping validation to its row and clears it on Edit', async () => {
    const { list, captured } = handlers([ACC_SOURCE, OPUS_SOURCE])
    server.use(...list)
    const user = userEvent.setup()
    renderPage()
    const opus = await screen.findByTestId('aifx-app-app-opus')
    await user.click(within(opus).getByRole('button', { name: 'Validate' }))
    expect(await screen.findByRole('heading', { name: 'Validation: OPUS' })).toBeInTheDocument()
    expect(screen.getByTestId('aifx-preview')).toBeInTheDocument()
    expect(captured.preview).toEqual([null])

    // Editing a different row clears the result, so it is not mistaken for that row's.
    const acc = screen.getByTestId('aifx-app-app-acc')
    await user.click(within(acc).getByRole('button', { name: 'Edit' }))
    expect(screen.getByRole('form', { name: 'Edit application mapping' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /^Validation:/ })).not.toBeInTheDocument()
    expect(screen.queryByTestId('aifx-preview')).not.toBeInTheDocument()
  })

  it('clears a saved-mapping validation when that row is removed', async () => {
    const { list, captured } = handlers([ACC_SOURCE, OPUS_SOURCE])
    server.use(...list)
    const user = userEvent.setup()
    renderPage()
    const acc = await screen.findByTestId('aifx-app-app-acc')
    await user.click(within(acc).getByRole('button', { name: 'Validate' }))
    expect(await screen.findByRole('heading', { name: 'Validation: ACC' })).toBeInTheDocument()

    await user.click(within(acc).getByRole('button', { name: 'Remove' }))
    expect(screen.queryByRole('heading', { name: /^Validation:/ })).not.toBeInTheDocument()
    expect(screen.queryByTestId('aifx-preview')).not.toBeInTheDocument()
    await waitFor(() => expect(captured.deleted).toEqual(['app-acc']))
    await waitFor(() => expect(screen.queryByTestId('aifx-app-app-acc')).not.toBeInTheDocument())
    expect(screen.queryByTestId('aifx-preview')).not.toBeInTheDocument()
  })

  it('shows the disabled state and fetches nothing when the server reports features.aifx false', async () => {
    const requested: string[] = []
    const onRequest = ({ request }: { request: Request }) => {
      if (new URL(request.url).pathname.includes('/aifx')) requested.push(request.url)
    }
    server.events.on('request:start', onRequest)
    try {
      server.use(...handlers().list)
      renderPage([], false)
      expect(await screen.findByText(/not enabled on this server/)).toBeInTheDocument()
      expect(screen.getByText('AIFx context')).toBeInTheDocument()
      expect(screen.queryByRole('table', { name: 'Organizations' })).not.toBeInTheDocument()
      expect(screen.queryByRole('table', { name: 'Onboarded applications' })).not.toBeInTheDocument()
      // Give any stray query a chance to fire before asserting none did.
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(requested).toEqual([])
    } finally {
      server.events.removeListener('request:start', onRequest)
    }
  })

  it('renders an unknown connection id as plain text rather than a link', async () => {
    server.use(...handlers([ACC_SOURCE], [{ ...ORG, connection_id: 'conn-hidden' }]).list)
    renderPage()
    const orgs = await screen.findByRole('table', { name: 'Organizations' })
    // Wait for the connections index to load so the fallback is not just the loading state.
    await waitFor(() => expect(within(orgs).getByText('conn-hidden')).toBeInTheDocument())
    await new Promise((resolve) => setTimeout(resolve, 50))
    const cell = within(orgs).getByText('conn-hidden')
    expect(cell.tagName).toBe('SPAN')
    expect(cell.closest('a')).toBeNull()
    expect(within(orgs).queryByRole('link')).not.toBeInTheDocument()
  })
})
