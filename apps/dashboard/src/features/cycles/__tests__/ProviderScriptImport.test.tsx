import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'
import type { Cycle } from '../api'

const cycle: Cycle = {
  followups: [],
  id: 'cycle',
  name: 'Checkout',
  project_id: 'p',
  app_id: 'a',
  environment_id: 'e',
  mode: 'live',
  owner: 'engineer',
  revision: 4,
  independent_review: false,
  created_at: '2026-09-07T12:00:00Z',
  updated_at: '2026-09-07T12:00:00Z',
  blockers: [],
  fields: {
    script: [
      {
        key: 'source',
        help: '',
        options: [],
        label: 'Script source',
        control: 'multiline',
        required: true,
      },
    ],
  },
  versions: [
    {
      id: 'plan',
      title: 'Checkout plan',
      kind: 'test_plan',
      content: {
        source: '{}',
        format: 'apex_native_json',
        engine: 'apex-load',
      },
      checksum: 'approved',
      dependencies: [],
      change_summary: 'Initial',
      status: 'approved',
      created_by: 'engineer',
      created_at: '2026-09-07T12:00:00Z',
      archived: false,
      reviews: [],
      approval_current: true,
      validation_issues: [],
      available_actions: [],
    },
  ],
}

const generation = '2026-09-07T12:00:00Z'
const exported = {
  id: 'remote',
  name: 'Checkout script',
  project_id: 'provider-project',
  version: '2',
  protocol: 'http',
  source: '{"actions":[]}',
  source_checksum: 'a'.repeat(64),
  content_scope: 'provider_export',
  executable_content_verified: false,
}
function mount(role: 'admin' | 'viewer' = 'admin') {
  server.use(
    http.get('*/v1/cycles/cycle', () => HttpResponse.json(cycle)),
    http.get('*/v1/catalog/applications', () => HttpResponse.json([])),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
    http.get('*/v1/cycles/cycle/provider-scripts', ({ request }) => {
      const query = new URL(request.url).searchParams
      return HttpResponse.json({
        revision: 4,
        connections: [
          {
            id: 'connection',
            name: 'Project load engine',
            provider: 'apex_load',
            runtime_version: generation,
          },
        ],
        scripts: query.has('connection_id') ? [exported] : [],
        preview: query.has('remote_script_id') ? exported : null,
        connection_version: generation,
        total: 1,
        imports: [],
        blockers: [],
      })
    }),
  )
  const state = authenticatedState(role)
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  renderApp({ initialEntries: ['/cycles/cycle?tab=script'], authState: state })
}
async function preview() {
  const user = userEvent.setup()
  await user.click(await screen.findByText('Import from the APEX Load library'))
  await user.selectOptions(
    await screen.findByLabelText('APEX Load connection'),
    'connection',
  )
  await screen.findByRole('option', { name: /Checkout script/ })
  await user.selectOptions(screen.getByLabelText('Provider script'), 'remote')
  await screen.findByRole('region', { name: 'Provider export preview' })
  return user
}
describe('Provider script import', () => {
  it('binds the preview and preserves details and operation identity after conflict', async () => {
    const submissions: Record<string, unknown>[] = []
    server.use(
      http.post(
        '*/v1/cycles/cycle/provider-scripts/import',
        async ({ request }) => {
          submissions.push((await request.json()) as Record<string, unknown>)
          if (submissions.length === 1)
            return HttpResponse.json(
              { detail: 'Cycle changed' },
              { status: 409 },
            )
          return HttpResponse.json({ version_id: 'imported-draft' })
        },
      ),
    )
    mount()
    const user = await preview()
    await user.selectOptions(
      screen.getByLabelText('Approved test plan'),
      'plan',
    )
    await user.type(
      screen.getByLabelText('Parameter files and dependencies'),
      'None required',
    )
    await user.type(
      screen.getByLabelText('Requirement and journey coverage'),
      'Checkout requirement',
    )
    await user.click(
      screen.getByRole('button', { name: 'Import export as draft' }),
    )
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Your draft details are retained',
    )
    expect(
      screen.getByLabelText('Requirement and journey coverage'),
    ).toHaveValue('Checkout requirement')
    await user.click(
      screen.getByRole('button', { name: 'Import export as draft' }),
    )
    expect(
      await screen.findByRole('link', { name: 'Review imported draft' }),
    ).toHaveAttribute('href', '/cycles/cycle?tab=script&version=imported-draft')
    await waitFor(() => expect(submissions).toHaveLength(2))
    expect(submissions[0]).toMatchObject({
      expected_revision: 4,
      connection_id: 'connection',
      connection_version: generation,
      remote_script_id: 'remote',
      expected_source_checksum: exported.source_checksum,
      plan_version_id: 'plan',
    })
    expect(submissions[1]).toEqual(submissions[0])
  })
  it('allows viewers to inspect exports without import controls', async () => {
    mount('viewer')
    await preview()
    expect(
      screen.getByText(/Executable content has not been verified/),
    ).toBeInTheDocument()
    expect(
      screen.queryByRole('button', { name: 'Import export as draft' }),
    ).not.toBeInTheDocument()
  })
})
