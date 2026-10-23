/**
 * /prompts browser: namespace tree + counts, archived toggle, search, the
 * slash-key URL encoding round-trip, the create flow and viewer gating.
 */
import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

import { promptCatalog } from './promptsTestHandlers'

// CodeMirror needs real DOM measurement; mock it as a controlled textarea
// (same boundary mock the hitl/new-test suites use).
vi.mock('@uiw/react-codemirror', async () => {
  const { createElement } = await import('react')
  return {
    default: ({
      value,
      onChange,
      editable,
      readOnly,
      'aria-label': ariaLabel,
    }: {
      value: string
      onChange?: (value: string) => void
      editable?: boolean
      readOnly?: boolean
      'aria-label'?: string
    }) =>
      createElement('textarea', {
        'data-testid': 'codemirror',
        'aria-label': ariaLabel,
        value,
        readOnly: readOnly === true || editable === false,
        onChange: (event: { target: { value: string } }) => onChange?.(event.target.value),
      }),
  }
})

describe('PromptsPage', () => {
  it('lists prompts by category in the order agents receive them, with readable titles', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    const filters = screen.getByRole('navigation', { name: 'Prompt categories' })
    expect(within(filters).getAllByRole('button').map((item) => item.getAttribute('aria-label'))).toEqual([
      'All prompts, 3',
      'System prompt, 0',
      'Application context, 1',
      'Environment context, 0',
      'Phase context, 2',
    ])
    expect(screen.getAllByRole('heading', { level: 2 }).slice(0, 2).map(heading => heading.textContent)).toEqual(['Application context, 1 prompt1', 'Phase context, 2 prompts2'])
    expect(screen.getByRole('region', { name: 'Phase context, 2 prompts' })).toBeInTheDocument()

    const storyRow = within(screen.getByTestId('prompt-row-p-story'))
    expect(storyRow.getByText('System prompt for story analysis')).toHaveClass('prompts-item-title')
    expect(storyRow.getByText('story_analysis/system')).toBeInTheDocument()
    expect(storyRow.getByText('Story Analysis')).toBeInTheDocument()
    expect(storyRow.getByText('v2')).toHaveClass('dash-context-chip')
    // Legacy templates and archived prompts stay out of the default list.
    expect(screen.queryByTestId('prompt-row-p-ops')).not.toBeInTheDocument()
    expect(screen.queryByTestId('prompt-row-p-retired')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'New test → Prompts' })).toHaveAttribute('href', '/runs/new?step=prompts')
  })

  it('previews the first row shown, not the first alphabetically, and replaces history on selection', async () => {
    server.use(...promptCatalog().handlers)
    const { router } = renderApp({ initialEntries: ['/prompts?ns=phase'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    // Pipeline order puts Story Analysis before Execution, though "execution/…" sorts first.
    const rows = screen.getAllByTestId(/^prompt-row-/).map(row => row.dataset.testid)
    expect(rows).toEqual(['prompt-row-p-story', 'prompt-row-p-exec'])
    expect(within(screen.getByTestId('prompt-row-p-story')).getByRole('button')).toHaveAttribute('aria-pressed', 'true')
    expect(within(screen.getByRole('complementary', { name: 'Prompt preview' })).getByRole('heading', { level: 2 })).toHaveTextContent('System prompt for story analysis')
    // One prompt per phase needs no group header.
    expect(screen.queryByRole('heading', { name: /^Story Analysis/ })).not.toBeInTheDocument()
    expect(screen.getByText(/Instructions for one pipeline phase/)).toBeInTheDocument()

    await userEvent.click(within(screen.getByTestId('prompt-row-p-exec')).getByRole('button'))
    expect(router.state.location.search).toContain('prompt=p-exec')
    expect(router.state.historyAction).toBe('REPLACE')
  })

  it('groups application context by application', async () => {
    server.use(...promptCatalog().handlers)
    const { router } = renderApp({ initialEntries: ['/prompts?ns=phase'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: /^Application context/ }))
    expect(router.state.location.search).toContain('ns=application')
    expect(await screen.findByRole('region', { name: 'Checkout service, 1 prompt' })).toBeInTheDocument()
    expect(within(screen.getByTestId('prompt-row-p-checkout-app')).getByText('checkout-service')).toBeInTheDocument()
  })

  it('reveals archived prompts and legacy templates only on request', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('checkbox', { name: /include archived/i }))
    await userEvent.click(screen.getByRole('checkbox', { name: 'Show legacy templates (2)' }))

    const legacy = await screen.findByRole('region', { name: 'Legacy templates, 2 prompts' })
    expect(legacy).toHaveTextContent('New tests can’t select these.')
    const archivedRow = within(screen.getByTestId('prompt-row-p-retired'))
    expect(archivedRow.getByText('archived')).toHaveClass('prompts-archived-chip')
    expect(screen.getByTestId('prompt-row-p-retired')).toHaveClass('prompts-row-archived')
    expect(screen.getByRole('button', { name: 'All prompts, 5' })).toBeInTheDocument()
  })

  it('filters by category and by debounced search, which also searches legacy templates', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts?prompt=p-story'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: /^Phase context/ }))
    expect(router.state.location.search).toContain('ns=phase')
    expect(router.state.location.search).not.toContain('prompt=')
    expect(screen.queryByTestId('prompt-row-p-checkout-app')).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /^All prompts/ }))
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search prompts' }), 'summarize')
    await waitFor(() => expect(router.state.location.search).toContain('q=summarize'))
    expect(router.state.location.search).not.toContain('prompt=')
    await waitFor(() => expect(screen.queryByTestId('prompt-row-p-story')).not.toBeInTheDocument())
    expect(await screen.findByTestId('prompt-row-p-ops')).toBeInTheDocument()
    expect(catalog.calls.listRequests.some((search) => search.includes('q=summarize'))).toBe(true)
  })

  it('explains an empty category and offers to create one there', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts?ns=environment'], authState: authenticatedState('admin', 'Dash Ops', []) })

    expect(await screen.findByText('No environment context yet')).toBeInTheDocument()
    expect(screen.getByText(/Facts about one environment/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear search' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'New environment context' }))
    expect(within(screen.getByRole('dialog', { name: 'New prompt' })).getByLabelText('Category')).toHaveValue('environment')
  })

  it('previews a slash-containing key with version status before the full editor', async () => {
    const catalog = promptCatalog()
    catalog.prompts[0]!.active_version_id = 'v-1'
    server.use(...catalog.handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    const row = within(await screen.findByTestId('prompt-row-p-story'))
    await userEvent.click(row.getByRole('button'))
    expect(router.state.location.pathname).toBe('/prompts')
    expect(router.state.location.search).toContain('prompt=p-story')
    expect(row.getByRole('button')).toHaveAttribute('aria-pressed', 'true')
    const preview = within(screen.getByRole('complementary', { name: 'Prompt preview' }))
    expect(await preview.findByTestId('codemirror')).toHaveValue('You are a story analyst.\nBe terse.\nReturn bullet points.')
    expect(await preview.findByText('v2 saved, not active')).toBeInTheDocument()
    expect(preview.getByText('2 versions')).toBeInTheDocument()
    // The description is the title; it is not repeated under it.
    expect(preview.getAllByText('System prompt for story analysis')).toHaveLength(1)
    expect(catalog.calls.listRequests).toHaveLength(1)

    await userEvent.click(preview.getByRole('link', { name: 'Open full editor' }))
    expect(router.state.location.pathname).toBe('/prompts/phase/story_analysis%2Fsystem')
  })

  it('creates a prompt from the panel and navigates to its detail', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: 'New prompt' }))
    const dialog = within(screen.getByRole('dialog', { name: 'New prompt' }))
    // With no category filter, application context is the usual thing to add.
    expect(dialog.getByLabelText('Category')).toHaveValue('application')

    await userEvent.selectOptions(dialog.getByLabelText('Category'), 'system')
    await userEvent.type(dialog.getByLabelText('Key'), 'triage/system')
    await userEvent.type(dialog.getByLabelText('Description'), 'Triage prompt')
    await userEvent.type(dialog.getByLabelText('Prompt content'), 'Sort by severity.')
    await userEvent.type(dialog.getByLabelText('Version note'), 'first cut')
    await userEvent.click(dialog.getByRole('button', { name: 'Create prompt (v1 active)' }))

    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/prompts/system/triage%2Fsystem'),
    )
    expect(catalog.calls.create).toEqual([
      {
        namespace: 'system',
        phase: null, application_id: null, environment_id: null, is_shared: false, is_default: false, selection_enabled: true,
        key: 'triage/system',
        content: 'Sort by severity.',
        description: 'Triage prompt',
        note: 'first cut',
      },
    ])
    const breadcrumb = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByText('triage/system')).toBeInTheDocument()
  })

  it('flags a duplicate key and can start from an existing prompt', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts?ns=phase'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: 'New prompt' }))
    const dialog = within(screen.getByRole('dialog', { name: 'New prompt' }))
    expect(dialog.getByLabelText('Category')).toHaveValue('phase')
    await userEvent.type(dialog.getByLabelText('Key'), 'story_analysis/system')
    expect(dialog.getByLabelText('Key')).toHaveAttribute('aria-invalid', 'true')
    expect(dialog.getByText('A phase context prompt with this key already exists.')).toBeInTheDocument()
    expect(dialog.getByRole('button', { name: 'Create prompt (v1 active)' })).toBeDisabled()

    await userEvent.selectOptions(dialog.getByLabelText('Start from an existing prompt'), 'p-story')
    await waitFor(() => expect(dialog.getByLabelText('Prompt content')).toHaveValue(
      'You are a story analyst.\nBe thorough.\nReturn bullet points.\nCite evidence for every claim.',
    ))
  })

  it('opens the preview as a sheet on a narrow page and returns focus to the row', async () => {
    class NarrowObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe() { this.callback([{ contentRect: { width: 600 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal('ResizeObserver', NarrowObserver)
    try {
      server.use(...promptCatalog().handlers)
      const { router } = renderApp({ initialEntries: ['/prompts?ns=phase'], authState: authenticatedState('admin', 'Dash Ops', []) })

      await screen.findByTestId('prompt-row-p-story')
      // Nothing is previewed until a row is chosen, so the list leads.
      expect(screen.queryByRole('complementary', { name: 'Prompt preview' })).not.toBeInTheDocument()
      await userEvent.click(within(screen.getByTestId('prompt-row-p-story')).getByRole('button'))
      expect(await screen.findByRole('dialog', { name: 'Prompt preview' })).toBeInTheDocument()
      // The sheet is a view of its own: Back closes it.
      await act(async () => { await router.navigate(-1) })
      await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Prompt preview' })).not.toBeInTheDocument())
      const execButton = within(screen.getByTestId('prompt-row-p-exec')).getByRole('button')
      await userEvent.click(execButton)
      const sheet = await screen.findByRole('dialog', { name: 'Prompt preview' })
      await waitFor(() => expect(within(sheet).getByRole('heading', { level: 2 })).toHaveFocus())
      await userEvent.click(within(sheet).getByRole('button', { name: 'Close preview' }))
      expect(screen.queryByRole('dialog', { name: 'Prompt preview' })).not.toBeInTheDocument()
      expect(router.state.location.search).not.toContain('prompt=')
      await waitFor(() => expect(within(screen.getByTestId('prompt-row-p-exec')).getByRole('button')).toHaveFocus())
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('shows the presets and tests that use a prompt', async () => {
    const tests = Array.from({ length: 5 }, (_, index) => ({
      thread_id: `thr-${index}`, title: index === 0 ? 'Checkout soak' : null, version: 2, slot: 'phase:story_analysis', recorded_at: '2026-06-10T10:00:00Z',
    }))
    server.use(...promptCatalog({
      usage: { 'p-story': { total_tests: 7, tests } },
      presets: [
        { assistant_id: 'preset-1', name: 'Peak certification', configurable: { prompt_selections: { schema_version: 1, system: null, phases: { story_analysis: { prompt_id: 'p-story', version_id: 'v-2' } }, application: [], environment: [] } } },
        { assistant_id: 'preset-2', name: 'Unrelated preset', configurable: {} },
      ],
    }).handlers)
    renderApp({ initialEntries: ['/prompts?ns=phase'], authState: authenticatedState('admin', 'Dash Ops', []) })

    const preview = within(await screen.findByRole('complementary', { name: 'Prompt preview' }))
    const usedBy = within(preview.getByRole('region', { name: 'Used by' }))
    expect(await usedBy.findByRole('link', { name: 'Peak certification' })).toHaveAttribute('href', '/test-presets/preset-1')
    expect(usedBy.queryByText('Unrelated preset')).not.toBeInTheDocument()
    expect(await usedBy.findByRole('link', { name: 'Checkout soak' })).toHaveAttribute('href', '/runs/thr-0')
    expect(usedBy.getAllByText(/v2 as story analysis phase/)).toHaveLength(5)
    expect(usedBy.getByText('and 2 more tests')).toBeInTheDocument()
  })

  it('says when nothing uses a prompt yet', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts?ns=phase'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByRole('complementary', { name: 'Prompt preview' })
    expect(await screen.findByText('No test preset selects this prompt, and no recorded test has pinned it yet.')).toBeInTheDocument()
  })

  it('sorts by most recently updated on request', async () => {
    server.use(...promptCatalog().handlers)
    const { router } = renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.selectOptions(screen.getByLabelText('Sort prompts'), 'updated')
    expect(router.state.location.search).toContain('sort=updated')
    const order = screen.getAllByTestId(/^prompt-row-/).map(row => row.dataset.testid ?? '')
    const updated = (id: string) => screen.getByTestId(id).querySelector('[title]')?.getAttribute('title') ?? ''
    expect(order.map(updated)).toEqual([...order.map(updated)].sort().reverse())
    expect(screen.queryByRole('heading', { level: 2, name: /Phase context/ })).not.toBeInTheDocument()
  })

  it('drops a link to a prompt that is not listed', async () => {
    server.use(...promptCatalog().handlers)
    const { router } = renderApp({ initialEntries: ['/prompts?prompt=p-ops'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    await waitFor(() => expect(router.state.location.search).not.toContain('prompt='))
  })

  it('checks keys against the whole catalog and protects typed content', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts?ns=phase&q=execution'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-exec')
    await userEvent.click(screen.getByRole('button', { name: 'New prompt' }))
    const dialog = within(screen.getByRole('dialog', { name: 'New prompt' }))
    // story_analysis/system isn't in the search results, but the key is still taken.
    await userEvent.type(dialog.getByLabelText('Key'), 'story_analysis/system')
    expect(await dialog.findByText('A phase context prompt with this key already exists.')).toBeInTheDocument()

    await userEvent.type(dialog.getByLabelText('Prompt content'), 'My own notes')
    await userEvent.selectOptions(await dialog.findByLabelText('Start from an existing prompt'), 'p-story')
    expect(dialog.getByRole('alert')).toHaveTextContent('Replace what you’ve written')
    await userEvent.click(dialog.getByRole('button', { name: 'Keep mine' }))
    expect(dialog.getByLabelText('Prompt content')).toHaveValue('My own notes')

    await userEvent.selectOptions(dialog.getByLabelText('Start from an existing prompt'), 'p-story')
    await userEvent.click(dialog.getByRole('button', { name: 'Replace content' }))
    await waitFor(() => expect(dialog.getByLabelText('Prompt content')).toHaveValue(
      'You are a story analyst.\nBe thorough.\nReturn bullet points.\nCite evidence for every claim.',
    ))
    // Back to blank clears the untouched copy.
    await userEvent.selectOptions(dialog.getByLabelText('Start from an existing prompt'), '')
    expect(dialog.getByLabelText('Prompt content')).toHaveValue('')
  })

  it('hides the New prompt button from viewers', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('viewer') })

    await screen.findByTestId('prompt-row-p-story')
    expect(screen.queryByRole('button', { name: 'New prompt' })).not.toBeInTheDocument()
  })
})
