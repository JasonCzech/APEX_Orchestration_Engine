import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import { ReleaseMappingPicker } from './ReleaseMappingPicker'
import { buildConfigurable, emptyDraft, type WizardDraft } from './wizardState'

const get = vi.hoisted(() => vi.fn())
vi.mock('@/api/apexClient', () => ({ getApexClient: () => ({ GET: get }) }))

it('persists exact release and revision, and clears them when changing connection', async () => {
  get.mockImplementation(async (path: string) => ({ response: { ok: true }, data: path.endsWith('/capabilities')
    ? { release_mapping: { application_id: 'load-app', revision: 'v3', releases: [{ id: 'rel-1', name: 'September' }] } }
    : { items: [{ id: 'c1', name: 'Mapped Load', project_id: 'p1' }, { id: 'c2', name: 'Other project', project_id: 'p2' }] } }))
  function Harness() {
    const [draft, setDraft] = useState(() => ({ ...emptyDraft(), scope: { ...emptyDraft().scope, project_id: 'p1' } }))
    return <><ReleaseMappingPicker draft={draft} onChange={setDraft} maxContextPackets={10} /><output data-testid="config">{JSON.stringify(buildConfigurable(draft))}</output></>
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  const user = userEvent.setup()
  await screen.findByRole('option', { name: 'Mapped Load' })
  expect(screen.queryByRole('option', { name: 'Other project' })).not.toBeInTheDocument()
  await user.selectOptions(screen.getByLabelText('Load connection'), 'c1')
  await user.selectOptions(await screen.findByLabelText('Release'), 'rel-1')
  expect(JSON.parse(screen.getByTestId('config').textContent!)).toMatchObject({ connections: { execution_engine: 'c1' }, load_test: { release_id: 'rel-1', mapping_revision: 'v3' } })
  await user.selectOptions(screen.getByLabelText('Load connection'), '')
  expect(JSON.parse(screen.getByTestId('config').textContent!).load_test).not.toHaveProperty('release_id')
  expect(JSON.parse(screen.getByTestId('config').textContent!).connections).not.toHaveProperty('execution_engine')
})

it('shows an unavailable saved connection truthfully until the user explicitly replaces it', async () => {
  get.mockResolvedValue({ response: { ok: true }, data: { items: [] } })
  function Harness() {
    const [draft, setDraft] = useState<WizardDraft>(() => {
      const initial = emptyDraft()
      return { ...initial, scope: { ...initial.scope, project_id: 'p1' }, config: {
        ...initial.config, golden_configurable: { connections: { execution_engine: 'removed' },
          load_test: { release_id: 'rel-1', mapping_revision: 'v1' } },
      } }
    })
    return <><ReleaseMappingPicker draft={draft} onChange={setDraft} /><output data-testid="config">{JSON.stringify(buildConfigurable(draft))}</output></>
  }
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  // Cached capabilities must not make an unavailable connection usable.
  client.setQueryData(['release-mapping', 'p1', 'removed'], {
    application_id: 'app-1', revision: 'v1', releases: [{ id: 'rel-1', name: 'Old release' }],
  })
  render(<QueryClientProvider client={client}><Harness /></QueryClientProvider>)
  await screen.findByRole('alert')
  expect(screen.getByLabelText('Load connection')).toHaveValue('removed')
  expect(screen.queryByLabelText('Release')).not.toBeInTheDocument()
  await userEvent.setup().selectOptions(screen.getByLabelText('Load connection'), '')
  const config = JSON.parse(screen.getByTestId('config').textContent!)
  expect(config.connections).not.toHaveProperty('execution_engine')
  expect(config.load_test).not.toHaveProperty('release_id')
  expect(config.load_test).not.toHaveProperty('mapping_revision')
})
