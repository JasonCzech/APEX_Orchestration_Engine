import { screen } from '@testing-library/react'
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
        integer: false,
        control: 'multiline',
        required: true,
      },
    ],
  },
  versions: [
    {
      id: 'script',
      title: 'Checkout source',
      kind: 'scope',
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

const ref = {
  id: '123',
  source: 'confluence',
  revision: '7',
  uri: 'https://docs.atlassian.net/wiki/spaces/42/pages/123',
}
function mount(role: 'admin' | 'viewer' = 'admin') {
  server.use(
    http.get('*/v1/cycles/cycle', () => HttpResponse.json(cycle)),
    http.get('*/v1/catalog/applications', () => HttpResponse.json([])),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
    http.get('*/v1/cycles/cycle/sources', ({ request }) => {
      const params = new URL(request.url).searchParams
      return HttpResponse.json({
        revision: 4,
        connections: [
          {
            id: 'docs',
            name: 'Engineering wiki',
            provider: 'confluence',
            runtime_version: '2026-09-07T12:00:00Z',
          },
        ],
        connection_version: '2026-09-07T12:00:00Z',
        blockers: [],
        hits: params.has('connection_id')
          ? [{ title: 'Checkout runbook', ref }]
          : [],
        document: params.has('page_id')
          ? {
              title: 'Checkout runbook',
              text: 'p95 below 150 ms',
              media_type: 'text/plain',
              ref,
            }
          : null,
        checksum: 'a'.repeat(64),
      })
    }),
  )
  const state = authenticatedState(role, 'Dash Ops', [])
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  renderApp({ initialEntries: ['/cycles/cycle?tab=scope'], authState: state })
}
describe('Connected scope sources', () => {
  it('captures the exact preview and navigates to a new scope draft', async () => {
    const user = userEvent.setup()
    let submitted: unknown
    server.use(
      http.post(
        '*/v1/cycles/cycle/scopes/script/sources',
        async ({ request }) => {
          submitted = await request.json()
          return HttpResponse.json({
            ...cycle,
            versions: [
              ...cycle.versions,
              { ...cycle.versions[0], id: 'captured', replaces: 'script' },
            ],
          })
        },
      ),
    )
    mount()
    await user.click(await screen.findByText('Find Confluence Cloud pages'))
    await user.selectOptions(
      await screen.findByLabelText('Confluence connection'),
      'docs',
    )
    await screen.findByRole('option', { name: /Checkout runbook/ })
    await user.selectOptions(screen.getByLabelText('Confluence page'), '123')
    await screen.findByRole('region', { name: 'Page revision preview' })
    await user.click(
      screen.getByRole('button', { name: 'Capture page as new scope draft' }),
    )
    expect(
      await screen.findByRole('link', { name: 'Open captured scope draft' }),
    ).toHaveAttribute('href', '/cycles/cycle?tab=scope&version=captured')
    expect(submitted).toMatchObject({
      expected_revision: 4,
      connection_id: 'docs',
      ref,
      expected_checksum: 'a'.repeat(64),
    })
  })
  it('allows viewer inspection without capture', async () => {
    const user = userEvent.setup()
    mount('viewer')
    await user.click(await screen.findByText('Find Confluence Cloud pages'))
    await user.selectOptions(
      await screen.findByLabelText('Confluence connection'),
      'docs',
    )
    await screen.findByRole('option', { name: /Checkout runbook/ })
    await user.selectOptions(screen.getByLabelText('Confluence page'), '123')
    await screen.findByRole('region', { name: 'Page revision preview' })
    expect(
      screen.queryByRole('button', { name: 'Capture page as new scope draft' }),
    ).not.toBeInTheDocument()
  })
})
