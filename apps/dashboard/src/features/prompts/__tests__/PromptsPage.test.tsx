/**
 * /prompts browser: namespace tree + counts, archived toggle, search, the
 * slash-key URL encoding round-trip, the create flow and viewer gating.
 */
import { screen, waitFor, within } from '@testing-library/react'
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
  it('renders the namespace tree with counts (phase pinned first) and hides archived rows', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('admin', 'Dash Ops', []) })

    // wait for the list to land before reading the tree counts
    await screen.findByTestId('prompt-row-p-story')
    const tree = screen.getByRole('navigation', { name: 'Namespaces' })
    const items = within(tree).getAllByRole('button')
    expect(items.map((item) => item.textContent)).toEqual([
      'All namespaces4',
      'Pipeline prompts2',
      'Application requirements1',
      'ops1',
    ])

    const storyRow = within(screen.getByTestId('prompt-row-p-story'))
    expect(storyRow.getByText('story_analysis/system')).toBeInTheDocument()
    expect(storyRow.getByText('v2')).toHaveClass('dash-context-chip')
    // archived prompt hidden until the toggle flips
    expect(screen.queryByTestId('prompt-row-p-retired')).not.toBeInTheDocument()
  })

  it('separates phase pairs and application prompts into named groups', async () => {
    server.use(...promptCatalog().handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts?ns=phase'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    await screen.findByTestId('prompt-row-p-story')
    expect(screen.getByRole('rowheader', { name: 'Pipeline prompt: Story analysis, 1 prompt' })).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: 'Pipeline prompt: Execution, 1 prompt' })).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /^Application requirements/ }))
    expect(router.state.location.search).toContain('ns=application')
    expect(await screen.findByRole('rowheader', { name: 'Application requirements: Checkout service requirements, 1 prompt' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'checkout-service' })).toBeInTheDocument()
  })

  it('include-archived toggle reveals archived rows with the archived chip', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('admin', 'Dash Ops', []) })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('checkbox', { name: /include archived/i }))

    const archivedRow = within(await screen.findByTestId('prompt-row-p-retired'))
    expect(archivedRow.getByText('archived')).toHaveClass('prompts-archived-chip')
    expect(screen.getByTestId('prompt-row-p-retired')).toHaveClass('prompts-row-archived')
  })

  it('filters by namespace from the tree and by debounced search (?ns / ?q)', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts?prompt=p-story'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: /^ops/ }))
    expect(router.state.location.search).toContain('ns=ops')
    expect(router.state.location.search).not.toContain('prompt=')
    expect(screen.queryByTestId('prompt-row-p-story')).not.toBeInTheDocument()
    expect(await screen.findByTestId('prompt-row-p-ops')).toBeInTheDocument()

    // back to all, then search server-side (q)
    await userEvent.click(screen.getByRole('button', { name: /^All namespaces/ }))
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search prompts' }), 'summarize')
    await waitFor(() => expect(router.state.location.search).toContain('q=summarize'))
    expect(router.state.location.search).not.toContain('prompt=')
    await waitFor(() =>
      expect(screen.queryByTestId('prompt-row-p-story')).not.toBeInTheDocument(),
    )
    expect(await screen.findByTestId('prompt-row-p-ops')).toBeInTheDocument()
    expect(catalog.calls.listRequests.some((search) => search.includes('q=summarize'))).toBe(true)
  })

  it('opens a slash-containing key in the catalog preview before the full editor', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts'],
      authState: authenticatedState('admin', 'Dash Ops', []),
    })

    const row = within(await screen.findByTestId('prompt-row-p-story'))
    await userEvent.click(row.getByRole('button', { name: 'story_analysis/system' }))
    expect(router.state.location.pathname).toBe('/prompts')
    expect(router.state.location.search).toContain('prompt=p-story')
    expect(row.getByRole('button', { name: 'story_analysis/system' })).toHaveAttribute('aria-pressed', 'true')
    expect(await within(screen.getByRole('complementary', { name: 'Prompt preview' })).findByTestId('codemirror')).toHaveValue(
      'You are a story analyst.\nBe thorough.\nReturn bullet points.\nCite evidence for every claim.',
    )
    expect(catalog.calls.listRequests).toHaveLength(1)

    await userEvent.click(screen.getByRole('link', { name: 'Open full editor' }))
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

    await userEvent.type(dialog.getByLabelText('Namespace'), 'ops')
    await userEvent.type(dialog.getByLabelText('Key'), 'triage/system')
    await userEvent.type(dialog.getByLabelText('Description'), 'Triage prompt')
    await userEvent.type(dialog.getByLabelText('Prompt content'), 'Sort by severity.')
    await userEvent.type(dialog.getByLabelText('Version note'), 'first cut')
    await userEvent.click(dialog.getByRole('button', { name: 'Create and activate prompt' }))

    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/prompts/ops/triage%2Fsystem'),
    )
    expect(catalog.calls.create).toEqual([
      {
        namespace: 'ops',
        key: 'triage/system',
        content: 'Sort by severity.',
        description: 'Triage prompt',
        note: 'first cut',
      },
    ])
    const breadcrumb = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByText('triage/system')).toBeInTheDocument()
  })

  it('hides the New prompt button from viewers', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState('viewer') })

    await screen.findByTestId('prompt-row-p-story')
    expect(screen.queryByRole('button', { name: 'New prompt' })).not.toBeInTheDocument()
  })
})
