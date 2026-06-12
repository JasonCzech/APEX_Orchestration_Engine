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
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState() })

    // wait for the list to land before reading the tree counts
    await screen.findByTestId('prompt-row-p-story')
    const tree = screen.getByRole('navigation', { name: 'Namespaces' })
    const items = within(tree).getAllByRole('button')
    expect(items.map((item) => item.textContent)).toEqual([
      'All namespaces3',
      'phase2',
      'ops1',
    ])

    const storyRow = within(screen.getByTestId('prompt-row-p-story'))
    expect(storyRow.getByText('story_analysis/system')).toBeInTheDocument()
    expect(storyRow.getByText('v2')).toHaveClass('dash-context-chip')
    // archived prompt hidden until the toggle flips
    expect(screen.queryByTestId('prompt-row-p-retired')).not.toBeInTheDocument()
  })

  it('include-archived toggle reveals archived rows with the archived chip', async () => {
    server.use(...promptCatalog().handlers)
    renderApp({ initialEntries: ['/prompts'], authState: authenticatedState() })

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
      initialEntries: ['/prompts'],
      authState: authenticatedState(),
    })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: /^ops/ }))
    expect(router.state.location.search).toContain('ns=ops')
    expect(screen.queryByTestId('prompt-row-p-story')).not.toBeInTheDocument()
    expect(screen.getByTestId('prompt-row-p-ops')).toBeInTheDocument()

    // back to all, then search server-side (q)
    await userEvent.click(screen.getByRole('button', { name: /^All namespaces/ }))
    await userEvent.type(screen.getByRole('searchbox', { name: 'Search prompts' }), 'summarize')
    await waitFor(() => expect(router.state.location.search).toContain('q=summarize'))
    await waitFor(() =>
      expect(screen.queryByTestId('prompt-row-p-story')).not.toBeInTheDocument(),
    )
    expect(screen.getByTestId('prompt-row-p-ops')).toBeInTheDocument()
    expect(catalog.calls.listRequests.some((search) => search.includes('q=summarize'))).toBe(true)
  })

  it('round-trips a slash-containing key through the encoded :name segment', async () => {
    server.use(...promptCatalog().handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts'],
      authState: authenticatedState(),
    })

    await userEvent.click(await screen.findByTestId('prompt-row-p-story'))
    expect(router.state.location.pathname).toBe('/prompts/phase/story_analysis%2Fsystem')

    // The detail page decodes the param back to the full key and resolves it.
    const breadcrumb = await screen.findByRole('navigation', { name: 'Breadcrumb' })
    expect(within(breadcrumb).getByText('story_analysis/system')).toBeInTheDocument()
    expect(screen.getByText('active v2')).toBeInTheDocument()
  })

  it('creates a prompt from the panel and navigates to its detail', async () => {
    const catalog = promptCatalog()
    server.use(...catalog.handlers)
    const { router } = renderApp({
      initialEntries: ['/prompts'],
      authState: authenticatedState('operator'),
    })

    await screen.findByTestId('prompt-row-p-story')
    await userEvent.click(screen.getByRole('button', { name: 'New prompt' }))
    const dialog = within(screen.getByRole('dialog', { name: 'New prompt' }))

    await userEvent.type(dialog.getByLabelText('Namespace'), 'ops')
    await userEvent.type(dialog.getByLabelText('Key'), 'triage/system')
    await userEvent.type(dialog.getByLabelText('Description'), 'Triage prompt')
    await userEvent.type(dialog.getByLabelText('Prompt content'), 'Sort by severity.')
    await userEvent.type(dialog.getByLabelText('Version note'), 'first cut')
    await userEvent.click(dialog.getByRole('button', { name: 'Create prompt' }))

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
