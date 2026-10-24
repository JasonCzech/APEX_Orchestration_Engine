import { screen, waitFor, within, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { authenticatedState, renderApp } from '@/test/render'
import * as helpContent from './content'

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

describe('dashboard help', () => {
  it('renders a readable guide inside the shell and navigates article anchors', async () => {
    const user = userEvent.setup()
    const { router } = renderApp({ initialEntries: ['/help'], authState: authenticatedState('operator') })
    expect(await screen.findByRole('heading', { name: 'Tests, runs, reviews, and decisions' })).toBeInTheDocument()
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Help & documentation')
    const outline = screen.getByRole('navigation', { name: 'On this page' })
    await user.click(within(outline).getByRole('link', { name: 'Roles and access' }))
    await waitFor(() => expect(router.state.location.hash).toBe('#roles-and-access'))
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Roles and access' })).toHaveFocus())
    expect(screen.queryByRole('link', { name: 'Help for this page' })).not.toBeInTheDocument()
  })

  it('searches full text, opens a result, and restores query state on Back', async () => {
    const user = userEvent.setup()
    const { router } = renderApp({ initialEntries: ['/help?q=human%20completion'], authState: authenticatedState('operator') })
    const results = await screen.findByRole('region', { name: 'Search results' })
    await user.click(await within(results).findByRole('link', { name: 'Run, review & complete phases' }))
    expect(await screen.findByRole('heading', { name: 'Complete human work' })).toBeInTheDocument()
    await act(async () => { await router.navigate(-1) })
    expect(await screen.findByRole('searchbox', { name: 'Search documentation' })).toHaveValue('human completion')
    await user.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(await screen.findByRole('heading', { name: 'Tests, runs, reviews, and decisions' })).toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: 'Search documentation' }), 'xyz-no-matching-article-313')
    expect(await screen.findByText(/0 matching articles/)).toBeInTheDocument()
  })

  it('positions anchored sections below the sticky header including its inset', async () => {
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('main-topbar')) return new DOMRect(0, 24, 1000, 56)
      return new DOMRect(0, 0, 1000, 800)
    })
    renderApp({ initialEntries: ['/help/welcome#roles-and-access'], authState: authenticatedState('viewer') })
    const heading = await screen.findByRole('heading', { name: 'Roles and access' })
    await waitFor(() => expect(heading).toHaveFocus())
    expect(heading.closest('article')?.parentElement?.style.getPropertyValue('--help-anchor-offset')).toBe('104px')
  })

  it('returns to the same section when its outline link is clicked again', async () => {
    const user = userEvent.setup()
    renderApp({ initialEntries: ['/help/welcome#roles-and-access'], authState: authenticatedState('viewer') })
    const heading = await screen.findByRole('heading', { name: 'Roles and access' })
    await waitFor(() => expect(heading).toHaveFocus())
    const scroll = vi.fn()
    heading.scrollIntoView = scroll
    await user.click(within(screen.getByRole('navigation', { name: 'On this page' })).getByRole('link', { name: 'Roles and access' }))
    await waitFor(() => expect(scroll).toHaveBeenCalledWith({ block: 'start', behavior: 'instant' }))
    expect(heading).toHaveFocus()
  })

  it('keeps a focused section visible when the header grows without moving other readers', async () => {
    let resize = () => {}
    let headerHeight = 56
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { resize = callback }
      observe() {}
      disconnect() {}
    })
    vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
      if (this.classList.contains('main-topbar')) return new DOMRect(0, 24, 1000, headerHeight)
      if (this.tagName === 'MAIN') return new DOMRect(0, 0, 1000, 800)
      return new DOMRect(0, 104, 100, 30)
    })
    renderApp({ initialEntries: ['/help/welcome#roles-and-access'], authState: authenticatedState('viewer') })
    const heading = await screen.findByRole('heading', { name: 'Roles and access' })
    await waitFor(() => expect(heading).toHaveFocus())
    const scroll = vi.fn()
    heading.scrollIntoView = scroll
    headerHeight = 126
    act(() => resize())
    expect(heading.closest('article')?.parentElement?.style.getPropertyValue('--help-anchor-offset')).toBe('174px')
    expect(scroll).toHaveBeenCalledOnce()
    screen.getByRole('searchbox', { name: 'Search documentation' }).focus()
    headerHeight = 180
    act(() => resize())
    expect(scroll).toHaveBeenCalledOnce()
  })

  it('preserves the shell skip target without reporting a missing article section', async () => {
    renderApp({ initialEntries: ['/help/welcome#main-content'], authState: authenticatedState('viewer') })
    await screen.findByRole('heading', { name: 'Roles and access' })
    expect(screen.queryByText(/That section is not in this build/)).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Skip to content' })).toHaveAttribute('href', '#main-content')
  })

  it('retains article nodes and focused headings across unrelated query changes', async () => {
    const { router } = renderApp({ initialEntries: ['/help/welcome#roles-and-access'], authState: authenticatedState('viewer') })
    const heading = await screen.findByRole('heading', { name: 'Roles and access' })
    await waitFor(() => expect(heading).toHaveFocus())
    await act(async () => { await router.navigate('/help/welcome?view=bookmarked#roles-and-access') })
    expect(screen.getByRole('heading', { name: 'Roles and access' })).toBe(heading)
    expect(heading).toHaveFocus()
  })

  it('keeps administration navigation and content appropriate to the current role', async () => {
    renderApp({ initialEntries: ['/help/access'], authState: authenticatedState('operator') })
    expect(await screen.findByRole('heading', { name: 'Administration guide requires an admin role' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Administration' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Create an API consumer' })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Read about roles & access' })).toHaveAttribute('href', '/help/welcome#roles-and-access')
  })

  it('shows administrative instructions to an admin', async () => {
    renderApp({ initialEntries: ['/help/access'], authState: authenticatedState() })
    expect(await screen.findByRole('heading', { name: 'Create an API consumer' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Administration' })).toBeInTheDocument()
  })

  it('recovers missing article and section deep links', async () => {
    const { router } = renderApp({ initialEntries: ['/help/not-in-build'], authState: authenticatedState('viewer') })
    expect(await screen.findByRole('heading', { name: 'Article not found' })).toBeInTheDocument()
    await act(async () => { await router.navigate('/help/scope#not-in-build') })
    expect(await screen.findByText(/That section is not in this build/)).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'On this page' })).toBeInTheDocument()
  })

  it('opens the bundled generated API contract and supports operation/schema searches', async () => {
    const user = userEvent.setup()
    renderApp({ initialEntries: ['/help/api-reference?operation=listPipelines'], authState: authenticatedState('viewer') })
    const reference = await screen.findByRole('region', { name: 'Generated domain API reference' })
    expect(within(reference).getByText('1 matching operations')).toBeInTheDocument()
    expect(within(reference).getByText('/v1/pipelines')).toBeInTheDocument()
    expect(within(reference).getByRole('link', { name: 'Download OpenAPI contract' })).toHaveAttribute('download', 'apex-v1.openapi.json')
    await user.selectOptions(within(reference).getByLabelText('Schema name'), 'StartPipelineRequest')
    expect(within(reference).getByRole('heading', { name: 'StartPipelineRequest' })).toBeInTheDocument()
    await user.clear(within(reference).getByRole('searchbox', { name: 'Search API operations' }))
    await user.type(within(reference).getByRole('searchbox', { name: 'Search API operations' }), 'nonexistent-operation-312')
    expect(within(reference).getByText('0 matching operations')).toBeInTheDocument()
  })

  it('recovers an unknown API group instead of silently hiding matching operations', async () => {
    renderApp({ initialEntries: ['/help/api-reference?tag=retired-group&operation=listPipelines'], authState: authenticatedState('viewer') })
    const reference = await screen.findByRole('region', { name: 'Generated domain API reference' })
    expect(within(reference).getByText('1 matching operations')).toBeInTheDocument()
    expect(within(reference).getByText(/retired-group.*not in this contract/)).toBeInTheDocument()
    expect(within(reference).getByRole('combobox', { name: 'API group' })).toHaveValue('')
  })

  it('retries a failed article chunk without losing its deep link', async () => {
    const user = userEvent.setup()
    vi.spyOn(helpContent, 'loadHelpSource').mockRejectedValueOnce(new Error('Chunk unavailable'))
    const { router } = renderApp({ initialEntries: ['/help/scope#primary-ownership-and-traffic-entry'], authState: authenticatedState('viewer') })
    await user.click(await screen.findByRole('button', { name: 'Retry article' }))
    expect(await screen.findByRole('heading', { name: 'Primary ownership and traffic entry' })).toBeInTheDocument()
    expect(router.state.location.hash).toBe('#primary-ownership-and-traffic-entry')
  })

  it('reports partial search failures and can retry missing content', async () => {
    const user = userEvent.setup()
    const load = helpContent.loadHelpSource
    let failed = false
    vi.spyOn(helpContent, 'loadHelpSource').mockImplementation(article => {
      if (article.id === 'scope' && !failed) { failed = true; return Promise.reject(new Error('Chunk unavailable')) }
      return load(article)
    })
    renderApp({ initialEntries: ['/help?q=traffic'], authState: authenticatedState('viewer') })
    await user.click(await screen.findByRole('button', { name: 'Retry search' }))
    const results = screen.getByRole('region', { name: 'Search results' })
    expect(await within(results).findByRole('link', { name: 'Define test scope' })).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Retry search' })).not.toBeInTheDocument())
  })
})
