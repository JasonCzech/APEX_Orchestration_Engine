/**
 * /test-presets list + organization preset page: presets of both kinds in one
 * table per application (F-15) with filters, viewer visibility, the settings
 * in the wizard's words, the start-a-test deep link into the wizard, and the
 * administrators' Edit JSON flow over SDK assistants.update.
 *
 * The LangGraph SDK boundary is vi.mocked per house pattern (see
 * features/new-test/__tests__/config.test.tsx) — msw only sees /v1 traffic.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

const { assistantsSearch, assistantsGet, assistantsUpdate } = vi.hoisted(() => ({
  assistantsSearch: vi.fn(),
  assistantsGet: vi.fn(),
  assistantsUpdate: vi.fn(),
}))

vi.mock('@/api/langgraphClient', () => ({
  getLangGraphClient: () =>
    Promise.resolve({
      assistants: { search: assistantsSearch, get: assistantsGet, update: assistantsUpdate },
    }),
}))

beforeEach(() => {
  vi.clearAllMocks()
  server.use(...wizardDraftHandlers())
})

const GOLDEN = {
  assistant_id: 'asst-gold',
  graph_id: 'pipeline',
  name: 'Nightly checkout soak',
  description: 'Pinned engine + custom gates',
  config: {
    configurable: {
      project_id: 'proj-alpha',
      app_id: 'app-checkout',
      engine: 'loadrunner',
      phases: ['story_analysis', 'test_planning', 'execution'],
      gates: { execution: { prompt_review: 'auto', output_review: 'auto' } },
      prompt_overrides: {
        'phase/execution': { content: 'Use the soak template' },
        'phase/reporting': { version_id: 'ver-2' },
      },
      limits: { max_revise_loops: 5 },
    },
  },
  context: {},
  metadata: { created_by: 'dash-ops' },
  created_at: '2026-06-01T00:00:00Z',
  updated_at: '2026-06-10T00:00:00Z',
  version: 3,
}

/** The langgraph dev server's auto-created default assistant (pins nothing). */
const SYSTEM_DEFAULT = {
  ...GOLDEN,
  assistant_id: 'asst-sys',
  name: 'pipeline',
  description: undefined,
  config: {},
  metadata: { created_by: 'system' },
  version: 1,
}

/** Quiet handlers for the wizard mount after the start-run navigation. */
function wizardDraftHandlers() {
  return [
    http.get('*/v1/catalog/applications', () =>
      HttpResponse.json([
        {
          id: 'app-checkout',
          project_ids: ['demo'],
          name: 'Checkout',
          description: 'Payments and cart funnel',
          archived_at: null,
          created_at: '2026-06-01T00:00:00Z',
          updated_at: '2026-06-01T00:00:00Z',
        },
      ]),
    ),
    http.get('*/v1/catalog/environments', () => HttpResponse.json([])),
    http.get('*/v1/documents', () =>
      HttpResponse.json({ items: [], limit: 50, offset: 0 }),
    ),
    http.get('*/v1/drafts', () => HttpResponse.json([])),
    http.post('*/v1/drafts', () =>
      HttpResponse.json(
        {
          id: 'draft-1',
          title: 'Untitled run',
          project_id: 'demo',
          payload: {},
          created_by: 'dash-ops',
          created_at: '2026-06-12T00:00:00Z',
          updated_at: '2026-06-12T00:00:00Z',
        },
        { status: 201 },
      ),
    ),
    http.put('*/v1/drafts/:id', () =>
      HttpResponse.json({
        id: 'draft-1',
        title: 'Untitled run',
        project_id: 'demo',
        payload: {},
        created_by: 'dash-ops',
        created_at: '2026-06-12T00:00:00Z',
        updated_at: '2026-06-12T00:00:00Z',
      }),
    ),
    http.get('*/v1/prompts', () => HttpResponse.json([])),
    http.get('*/v1/work-tracking/saved-queries', () =>
      HttpResponse.json({ items: [], limit: 50, offset: 0 }),
    ),
  ]
}

const TEAM_ROW = {
  id: 'preset-team', name: 'Checkout smoke', description: 'Quick look before a release', project_id: 'demo',
  application_id: 'app-checkout', visibility: 'project', payload: { schema_version: 1, sections: { phases: { phases: null, schema_version: 2, gates_mode: 'all_auto' } } },
  version: 1, created_by: 'Priya', updated_by: 'Priya', created_at: '2026-06-01T00:00:00Z', updated_at: '2026-06-01T00:00:00Z',
  archived_at: null, mine: false, editable: true,
}
const GENERAL_ROW = { ...TEAM_ROW, id: 'preset-general', name: 'Review everything', description: null, application_id: null, mine: true,
  payload: { schema_version: 1, sections: { phases: { phases: null, schema_version: 2, gates_mode: 'all_gated' } } } }

function installTeamPresets(rows: unknown[] = [TEAM_ROW, GENERAL_ROW]) {
  server.use(
    http.get('*/v1/test-presets', () => HttpResponse.json(rows)),
    http.get('*/v1/test-presets/usage', ({ request }) => {
      const kind = new URL(request.url).searchParams.get('kind')
      return HttpResponse.json(kind === 'organization' ? [{ preset_id: 'asst-gold', tests: 4, last_used_at: '2026-06-11T00:00:00Z' }] : [])
    }),
  )
}

describe('PresetsListPage', () => {
  it('hydrates authorized details when search returns only the permitted summary fields', async () => {
    assistantsSearch.mockResolvedValue([{ assistant_id: GOLDEN.assistant_id, graph_id: 'pipeline', name: GOLDEN.name, version: 3 }])
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({ initialEntries: ['/test-presets'], authState: authenticatedState() })
    const row = await screen.findByTestId('preset-row-organization:asst-gold')
    expect(assistantsGet).toHaveBeenCalledWith('asst-gold', { signal: expect.any(AbortSignal) })
    expect(within(row).getByText('LoadRunner')).toBeInTheDocument()
  })

  it('lists both kinds in one table per application, General last, without the system default', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN, SYSTEM_DEFAULT])
    installTeamPresets()
    renderApp({ initialEntries: ['/test-presets'], authState: authenticatedState() })

    const checkout = await screen.findByRole('region', { name: 'Presets for Checkout' })
    const rows = within(checkout).getAllByRole('row').slice(1)
    expect(rows.map(row => within(row).getByRole('link').textContent)).toEqual(['Checkout smoke', 'Nightly checkout soak'])
    const golden = within(checkout).getByTestId('preset-row-organization:asst-gold')
    expect(within(golden).getByText('Pinned engine + custom gates')).toBeInTheDocument()
    expect(within(golden).getByText('LoadRunner')).toBeInTheDocument()
    expect(within(golden).getByText('3 of 7 phases by agent')).toBeInTheDocument()
    expect(within(golden).getByText('Custom reviews')).toBeInTheDocument()
    expect(within(golden).getByText('Organization')).toBeInTheDocument()
    expect(await within(golden).findByText('4')).toBeInTheDocument()
    expect(within(golden).getByRole('link', { name: 'Nightly checkout soak' })).toHaveAttribute('href', '/test-presets/asst-gold')
    expect(within(checkout).getByRole('link', { name: 'Checkout smoke' })).toHaveAttribute('href', '/test-presets/team/preset-team')

    const regions = screen.getAllByRole('region').map(region => region.getAttribute('aria-label'))
    expect(regions.indexOf('General presets')).toBeGreaterThan(regions.indexOf('Presets for Checkout'))
    expect(within(screen.getByRole('region', { name: 'General presets' })).getByText('Review everything')).toBeInTheDocument()
    // The auto-created pipeline assistant pins nothing, so it is not offered as a preset.
    expect(screen.queryByText('pipeline')).not.toBeInTheDocument()
  })

  it('filters by application, search and who can use it, and clears back to everything', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN])
    installTeamPresets()
    const user = userEvent.setup()
    const { router } = renderApp({ initialEntries: ['/test-presets?application=general'], authState: authenticatedState() })

    expect(await screen.findByRole('region', { name: 'General presets' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Presets for Checkout' })).not.toBeInTheDocument()

    await user.selectOptions(screen.getByRole('combobox', { name: 'Application' }), '')
    await user.selectOptions(screen.getByRole('combobox', { name: 'Who can use it' }), 'mine')
    expect(screen.getByText('Review everything')).toBeInTheDocument()
    expect(screen.queryByText('Checkout smoke')).not.toBeInTheDocument()
    expect(router.state.location.search).toContain('visibility=mine')

    await user.type(screen.getByRole('searchbox', { name: 'Search presets' }), 'soak')
    expect(await screen.findByText('No matching presets')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show all presets' }))
    expect(await screen.findByText('Nightly checkout soak')).toBeInTheDocument()
    expect(screen.getByText('Checkout smoke')).toBeInTheDocument()
  })

  it('explains how to create the first preset, offering it only to people who can start tests', async () => {
    assistantsSearch.mockResolvedValue([])
    const { unmount } = renderApp({ initialEntries: ['/test-presets'], authState: authenticatedState('operator') })
    expect(await screen.findByText('No test presets yet')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Create a preset' })).toHaveAttribute('href', '/runs/new')
    unmount()

    renderApp({ initialEntries: ['/test-presets'], authState: authenticatedState('viewer') })
    expect(await screen.findByText('No test presets yet')).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Create a preset' })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'New preset' })).not.toBeInTheDocument()
  })

  it('is viewer-visible (read access does not require admin)', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN])
    renderApp({ initialEntries: ['/test-presets'], authState: authenticatedState('viewer') })
    expect(await screen.findByTestId('preset-row-organization:asst-gold')).toBeInTheDocument()
  })

  it('redirects the old /golden-configs address', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN])
    const { router } = renderApp({ initialEntries: ['/golden-configs'], authState: authenticatedState() })
    await waitFor(() => expect(router.state.location.pathname).toBe('/test-presets'))
  })
})

describe('organization preset page', () => {
  it('shows its settings in the wizard\'s words, its usage, and the raw configurable', async () => {
    server.use(http.get('*/v1/catalog/projects', () => HttpResponse.json([{ project_id: 'proj-alpha', name: 'Payments project' }])))
    installTeamPresets()
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({ initialEntries: ['/test-presets/asst-gold'], authState: authenticatedState('admin', 'Dash Ops', []) })

    expect(await screen.findByRole('heading', { name: 'Nightly checkout soak' })).toBeInTheDocument()
    const facts = screen.getByTestId('preset-facts')
    expect(facts).toHaveTextContent('v3')
    // Names, not ids, once the catalog loads.
    await waitFor(() => expect(facts).toHaveTextContent('For Checkout · Payments project'))

    const settings = screen.getByLabelText('Preset settings')
    const field = (label: string) => within(settings).getByText(label).closest('div') as HTMLElement
    expect(field('Engine')).toHaveTextContent('LoadRunner')
    expect(field('Review policy')).toHaveTextContent('Custom reviews')
    expect(await screen.findByText(/Started 4 tests you can see/)).toBeInTheDocument()

    const raw = screen.getByTestId('gc-raw-json')
    expect(within(raw).getByText('Raw configurable JSON')).toBeInTheDocument()
    expect(raw).toHaveTextContent('"engine": "loadrunner"')
  })

  it('starts a test from the preset: the wizard opens filled in and strips the one-shot param', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    assistantsSearch.mockResolvedValue([GOLDEN])
    const user = userEvent.setup()
    const { router, unmount } = renderApp({ initialEntries: ['/test-presets/asst-gold'], authState: authenticatedState() })

    const start = await screen.findByRole('link', { name: 'Start a test from this preset' })
    expect(start).toHaveAttribute('href', '/runs/new?preset=organization%3Aasst-gold')
    await user.click(start)

    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/new'))
    const bar = await screen.findByTestId('preset-bar')
    await waitFor(() => expect(bar).toHaveTextContent('Nightly checkout soak'))
    expect(bar).toHaveTextContent('no changes')
    await waitFor(() => expect(router.state.location.search).not.toContain('preset='))
    await user.click(screen.getByRole('tab', { name: 'Config' }))
    expect(screen.getByRole('radio', { name: /LoadRunner/ })).toHaveAttribute('aria-checked', 'true')
    await user.click(screen.getByRole('button', { name: 'Save draft' }))
    await screen.findByText('Draft saved')
    unmount()
  })

  it('hides launch and edit controls from viewers', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({ initialEntries: ['/test-presets/asst-gold'], authState: authenticatedState('viewer') })

    expect(await screen.findByRole('heading', { name: 'Nightly checkout soak' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Start a test from this preset' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit JSON (advanced)' })).not.toBeInTheDocument()
    expect(screen.getByText('Organization presets are maintained by platform administrators.')).toBeInTheDocument()
  })

  it('lets operators start and duplicate, but reserves edits for platform administrators', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({ initialEntries: ['/test-presets/asst-gold'], authState: authenticatedState('operator') })

    expect(await screen.findByRole('link', { name: 'Start a test from this preset' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Duplicate' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit in the wizard' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit JSON (advanced)' })).not.toBeInTheDocument()
    expect(screen.getByText('Organization presets are maintained by platform administrators.')).toBeInTheDocument()
  })

  it('Edit JSON validates the parse and saves a new version via assistants.update', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    assistantsUpdate.mockResolvedValue({ ...GOLDEN, version: 4, config: { configurable: { engine: 'sim' } } })
    const user = userEvent.setup()
    renderApp({ initialEntries: ['/test-presets/asst-gold'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await user.click(await screen.findByRole('button', { name: 'Edit JSON (advanced)' }))
    const editor = screen.getByRole('textbox', { name: 'Configurable JSON' })
    expect(editor).toHaveValue(JSON.stringify(GOLDEN.config.configurable, null, 2))

    const save = screen.getByRole('button', { name: 'Save new version' })
    await user.clear(editor)
    await user.paste('not json')
    expect(screen.getByRole('alert')).toHaveTextContent(/invalid json/i)
    expect(save).toBeDisabled()
    expect(assistantsUpdate).not.toHaveBeenCalled()

    await user.clear(editor)
    await user.paste('{"engine": "sim"}')
    expect(save).toBeEnabled()
    await user.click(save)

    await waitFor(() =>
      expect(assistantsUpdate).toHaveBeenCalledWith('asst-gold', { config: { configurable: { engine: 'sim' } } }),
    )
    // Back on the read view, with the bumped version from the response.
    expect(await screen.findByTestId('gc-raw-json')).toBeInTheDocument()
    expect(screen.getByTestId('preset-facts')).toHaveTextContent('v4')
  })

  it('keeps the same assistant locked after a route remount while its save is pending', async () => {
    assistantsGet.mockImplementation((assistantId: string) =>
      Promise.resolve(assistantId === SYSTEM_DEFAULT.assistant_id ? SYSTEM_DEFAULT : GOLDEN),
    )
    assistantsSearch.mockResolvedValue([GOLDEN, SYSTEM_DEFAULT])
    let markSaveStarted!: () => void
    const saveStarted = new Promise<void>((resolve) => { markSaveStarted = resolve })
    let releaseSave!: () => void
    const saveRelease = new Promise<void>((resolve) => { releaseSave = resolve })
    assistantsUpdate.mockImplementationOnce(async () => {
      markSaveStarted()
      await saveRelease
      return { ...GOLDEN, version: 4, config: { configurable: { engine: 'sim' } } }
    })
    const user = userEvent.setup()
    const { router } = renderApp({ initialEntries: ['/test-presets/asst-gold'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await user.click(await screen.findByRole('button', { name: 'Edit JSON (advanced)' }))
    const editor = screen.getByRole('textbox', { name: 'Configurable JSON' })
    await user.clear(editor)
    await user.paste('{"engine":"sim"}')
    await user.click(screen.getByRole('button', { name: 'Save new version' }))
    await saveStarted

    await act(async () => router.navigate('/test-presets/asst-sys'))
    await screen.findByRole('heading', { name: 'pipeline' })
    expect(screen.getByRole('button', { name: 'Edit JSON (advanced)' })).toBeEnabled()
    expect(screen.getByTestId('gc-system-chip')).toHaveTextContent('system default')

    await act(async () => router.navigate('/test-presets'))
    await screen.findByTestId('preset-row-organization:asst-gold')
    await act(async () => router.navigate('/test-presets/asst-gold'))

    expect(await screen.findByText('saving…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit JSON (advanced)' })).toBeDisabled()
    expect(assistantsUpdate).toHaveBeenCalledTimes(1)

    await act(async () => {
      releaseSave()
      await saveRelease
    })

    await waitFor(() => expect(screen.getByTestId('preset-facts')).toHaveTextContent('v4'))
    expect(screen.queryByText('saving…')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Edit JSON (advanced)' })).toBeEnabled()
  })

  it('drops the JSON draft when a cached assistant route replaces the editor', async () => {
    assistantsGet.mockImplementation((assistantId: string) =>
      Promise.resolve(assistantId === SYSTEM_DEFAULT.assistant_id ? SYSTEM_DEFAULT : GOLDEN),
    )
    const user = userEvent.setup()
    const { router } = renderApp({ initialEntries: ['/test-presets/asst-sys'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByRole('heading', { name: 'pipeline' })
    await act(async () => router.navigate('/test-presets/asst-gold'))
    await screen.findByRole('heading', { name: 'Nightly checkout soak' })
    await user.click(screen.getByRole('button', { name: 'Edit JSON (advanced)' }))
    await user.click(screen.getByText('Advanced JSON'))
    const editor = screen.getByRole('textbox', { name: 'Configurable JSON' })
    await user.clear(editor)
    await user.paste('{"engine":"A-only"}')

    await act(async () => router.navigate('/test-presets/asst-sys'))
    await screen.findByRole('heading', { name: 'pipeline' })
    expect(screen.queryByRole('textbox', { name: 'Configurable JSON' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: 'Edit JSON (advanced)' }))
    await user.click(screen.getByText('Advanced JSON'))
    expect(screen.getByRole('textbox', { name: 'Configurable JSON' })).toHaveValue('{}')
  })
})
