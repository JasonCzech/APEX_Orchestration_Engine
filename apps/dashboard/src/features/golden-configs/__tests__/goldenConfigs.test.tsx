/**
 * /golden-configs list + detail: summary chips, system-default chip, the
 * structured read view (gate matrix + raw JSON), viewer visibility (golden
 * configs are a viewer-visible read surface), start-run deep-link into the
 * wizard's Config step (?golden= preselect), and the Edit JSON flow over SDK
 * assistants.update.
 *
 * The LangGraph SDK boundary is vi.mocked per house pattern (see
 * features/new-test/__tests__/config.test.tsx) — msw only sees /v1 traffic.
 */
import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it, vi } from 'vitest'

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
          project_id: 'demo',
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

describe('GoldenConfigsPage', () => {
  it('renders config summary chips and marks the system default', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN, SYSTEM_DEFAULT])
    renderApp({ initialEntries: ['/golden-configs'], authState: authenticatedState() })

    const card = await screen.findByTestId('gc-card-asst-gold')
    expect(within(card).getByText('Nightly checkout soak')).toBeInTheDocument()
    expect(within(card).getByText('Pinned engine + custom gates')).toBeInTheDocument()
    expect(within(card).getByText('LoadRunner')).toBeInTheDocument()
    expect(within(card).getByText('custom gates')).toBeInTheDocument()
    expect(within(card).getByText('3 phases')).toBeInTheDocument()
    expect(within(card).getByText('2 prompt pins')).toBeInTheDocument()
    expect(within(card).getByText('v3')).toBeInTheDocument()
    expect(within(card).queryByTestId('gc-system-chip')).not.toBeInTheDocument()

    // The auto-created pipeline assistant stays listed, flagged as system default.
    const systemCard = screen.getByTestId('gc-card-asst-sys')
    expect(within(systemCard).getByTestId('gc-system-chip')).toHaveTextContent('system default')
    expect(within(systemCard).getByText('Simulated')).toBeInTheDocument()
    expect(within(systemCard).getByText('all gated')).toBeInTheDocument()
    expect(within(systemCard).getByText('7 phases')).toBeInTheDocument()
    expect(within(systemCard).getByText('0 prompt pins')).toBeInTheDocument()
  })

  it('is viewer-visible (read access does not require admin)', async () => {
    assistantsSearch.mockResolvedValue([GOLDEN])
    renderApp({ initialEntries: ['/golden-configs'], authState: authenticatedState('viewer') })

    expect(await screen.findByTestId('gc-card-asst-gold')).toBeInTheDocument()
  })
})

describe('GoldenConfigDetailPage', () => {
  it('renders the structured read view: scope, gate matrix, pins, limits, raw JSON', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({
      initialEntries: ['/golden-configs/asst-gold'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    expect(await screen.findByRole('heading', { name: 'Nightly checkout soak' })).toBeInTheDocument()

    const scope = screen.getByRole('region', { name: 'Scope defaults' })
    expect(within(scope).getByText('proj-alpha')).toBeInTheDocument()
    expect(within(scope).getByText('app-checkout')).toBeInTheDocument()
    expect(within(scope).getByText('—')).toBeInTheDocument() // environment not pinned

    // Compact 7x2 read-only matrix: execution's pinned auto pair, everything else gated.
    const matrix = screen.getByTestId('gc-gate-matrix')
    expect(within(matrix).getAllByText('auto')).toHaveLength(2)
    expect(within(matrix).getAllByText('gated')).toHaveLength(12)
    const executionRow = within(matrix).getByText('execution').closest('tr') as HTMLElement
    expect(within(executionRow).getAllByText('auto')).toHaveLength(2)

    const pins = screen.getByRole('region', { name: 'Prompt overrides' })
    expect(within(pins).getByText('phase/execution')).toBeInTheDocument()
    expect(within(pins).getByText('inline content')).toBeInTheDocument()
    expect(within(pins).getByText('phase/reporting')).toBeInTheDocument()
    expect(within(pins).getByText('version ver-2')).toBeInTheDocument()

    const limits = screen.getByRole('region', { name: 'Limits' })
    const reviseRow = within(limits).getByText('Max revise loops').closest('div') as HTMLElement
    expect(within(reviseRow).getByText('5')).toBeInTheDocument()
    expect(within(reviseRow).queryByText('(default)')).not.toBeInTheDocument()
    const turnsRow = within(limits).getByText('Max dialogue turns').closest('div') as HTMLElement
    expect(turnsRow).toHaveTextContent('20')
    expect(turnsRow).toHaveTextContent('(default)')

    const raw = screen.getByTestId('gc-raw-json')
    expect(within(raw).getByText('Raw configurable JSON')).toBeInTheDocument()
    expect(raw).toHaveTextContent('"engine": "loadrunner"')
  })

  it('start-run deep-links the wizard Config step and preselects the golden config', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    assistantsSearch.mockResolvedValue([GOLDEN, SYSTEM_DEFAULT])
    server.use(...wizardDraftHandlers())
    const user = userEvent.setup()
    const { router, unmount } = renderApp({
      initialEntries: ['/golden-configs/asst-gold'],
      authState: authenticatedState(),
    })

    await user.click(await screen.findByRole('button', { name: 'Start run with this config' }))

    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/new'))
    expect(router.state.location.search).toContain('step=config')

    // The wizard's Config step applied the bundle (inherited chip + pinned engine)…
    expect(await screen.findByTestId('config-inherited-chip')).toHaveTextContent('config inherited')
    expect(screen.getByRole('radio', { name: /LoadRunner/ })).toHaveAttribute('aria-checked', 'true')
    // …and the one-shot param is stripped so Clear sticks.
    await waitFor(() => expect(router.state.location.search).not.toContain('golden='))
    await user.click(screen.getByRole('button', { name: 'Save Draft' }))
    await screen.findByText('Draft saved')
    unmount()
  })

  it('hides mutation and launch controls from viewers', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({
      initialEntries: ['/golden-configs/asst-gold'],
      authState: authenticatedState('viewer'),
    })

    expect(await screen.findByRole('heading', { name: 'Nightly checkout soak' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit JSON' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Start run with this config' })).not.toBeInTheDocument()
  })

  it('lets operators start runs but reserves assistant edits for admins', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    renderApp({
      initialEntries: ['/golden-configs/asst-gold'],
      authState: authenticatedState('operator'),
    })

    expect(await screen.findByRole('heading', { name: 'Nightly checkout soak' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Edit JSON' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start run with this config' })).toBeInTheDocument()
  })

  it('Edit JSON validates the parse and saves a new version via assistants.update', async () => {
    assistantsGet.mockResolvedValue(GOLDEN)
    assistantsUpdate.mockResolvedValue({
      ...GOLDEN,
      version: 4,
      config: { configurable: { engine: 'sim' } },
    })
    const user = userEvent.setup()
    renderApp({
      initialEntries: ['/golden-configs/asst-gold'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    await user.click(await screen.findByRole('button', { name: 'Edit JSON' }))
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
      expect(assistantsUpdate).toHaveBeenCalledWith('asst-gold', {
        config: { configurable: { engine: 'sim' } },
      }),
    )
    // Back on the read view, with the bumped version from the response.
    expect(await screen.findByTestId('gc-gate-matrix')).toBeInTheDocument()
    expect(screen.getByText('v4')).toBeInTheDocument()
  })
})
