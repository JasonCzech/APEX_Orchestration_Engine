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
      kind: 'script',
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
function mount(role: 'admin' | 'viewer' = 'admin', storedSize?: number) {
  server.use(
    http.get('*/v1/cycles/cycle', () => HttpResponse.json(cycle)),
    http.get('*/v1/cycles/cycle/scripts', () =>
      HttpResponse.json({
        revision: 4,
        artifacts: storedSize === undefined ? [] : [{ id: 'artifact', version_id: 'script', source_checksum: 'checksum', size_bytes: storedSize, stored_at: '2026-09-07T12:00:00Z', connection_id: 'store' }],
        connections: [
          {
            id: 'store',
            name: 'Project evidence',
            provider: 's3',
            runtime_version: '2026-09-07T12:00:00Z',
          },
        ],
      }),
    ),
    http.get('*/v1/catalog/applications', () => HttpResponse.json([])),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
  )
  const state = authenticatedState(role, 'Dash Ops', [])
  if (state.status === 'authenticated')
    state.systemInfo.features = { cycles: true }
  renderApp({ initialEntries: ['/cycles/cycle?tab=script'], authState: state })
}

describe('Script source storage', () => {
  it('uses shared readable units for retained source size', async () => {
    mount('viewer', 2048)
    expect(await screen.findByText(/2.0 KB · source SHA-256/)).toBeVisible()
  })
  it('binds storage to the selected approved version and connection', async () => {
    const user = userEvent.setup()
    let submitted: unknown
    server.use(
      http.post(
        '*/v1/cycles/cycle/scripts/script/artifact',
        async ({ request }) => {
          submitted = await request.json()
          return HttpResponse.json({
            id: 'artifact',
            version_id: 'script',
            source_checksum: 'checksum',
            stored_at: '2026-09-07T12:00:00Z',
          })
        },
      ),
    )
    mount()
    await user.selectOptions(
      await screen.findByLabelText('Persistent artifact store'),
      'store',
    )
    await user.click(
      screen.getByRole('button', { name: 'Store approved source' }),
    )
    await waitFor(() =>
      expect(submitted).toEqual({
        expected_revision: 4,
        connection_id: 'store',
      }),
    )
    expect(
      await screen.findByText(/Approved source checksum verified/),
    ).toHaveAttribute('role', 'status')
    expect(
      screen.queryByRole('button', { name: /^Launch/ }),
    ).not.toBeInTheDocument()
  })
  it('does not offer viewers a storage mutation', async () => {
    mount('viewer')
    await screen.findByRole('heading', { name: 'Immutable script source' })
    expect(
      screen.queryByRole('button', { name: 'Store approved source' }),
    ).not.toBeInTheDocument()
  })
  it('loads a file into a new draft without changing the approved version', async () => {
    const user = userEvent.setup()
    mount()
    await user.click(
      await screen.findByRole('button', { name: 'Revise as new draft' }),
    )
    const source = '{\n  "name": "Checkout café"\n}\n'
    const file = new File([source], 'checkout.json', {
      type: 'application/json',
    })
    Object.defineProperty(file, 'arrayBuffer', {
      value: async () => new TextEncoder().encode(source).buffer,
    })
    await user.upload(screen.getByLabelText('Import UTF-8 script source'), file)
    await waitFor(() =>
      expect(screen.getByLabelText('Script source *')).toHaveValue(source),
    )
    expect(cycle.versions[0]?.content.source).toBe('{}')
    expect(screen.getByText(/Imported checkout.json/)).toHaveAttribute(
      'role',
      'status',
    )
  })
})
