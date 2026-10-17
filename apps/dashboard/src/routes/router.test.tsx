import { screen, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

describe('router', () => {
  it.each([
    ['/approvals', 'Tests', null],
    // /prompts became a real screen in D5; / became the real Home dashboard in
    // D7 (covered in features/home tests — its headings are content-driven);
    // /golden-configs became real in D7 too — /runs/compare is the last
    // placeholder-backed route.
    ['/runs/compare', 'Compare runs', 'Choose runs to compare'],
    ['/admin/system', 'System status', 'System'],
  ])('renders the %s page inside the shell', async (path, title, bodyTitle = title) => {
    renderApp({ initialEntries: [path], authState: authenticatedState() })

    // Placeholder body (h2) + topbar title from the route handle (h1).
    if (bodyTitle) expect(await screen.findByRole('heading', { level: 2, name: bodyTitle })).toBeInTheDocument()
    else await screen.findByRole('main')
    expect(screen.getByRole('heading', { level: 1, name: title })).toBeInTheDocument()
    expect(screen.getByTestId('sidebar')).toBeInTheDocument()
  })

  it('renders parameterized deep links', async () => {
    // No placeholder-backed param routes remain after D7, so the deep-link
    // smoke rides the real /golden-configs/:assistantId screen: a quiet
    // SDK-shaped handler satisfies the page fetch, and the assertions stay
    // routing-level (route-handle h1 + the page's name heading).
    server.use(
      http.get('*/assistants/:assistantId', ({ params }) =>
        HttpResponse.json({
          assistant_id: params['assistantId'],
          graph_id: 'pipeline',
          name: 'Deep-linked config',
          config: {},
          context: {},
          metadata: {},
          version: 1,
          created_at: '2026-06-01T00:00:00Z',
          updated_at: '2026-06-01T00:00:00Z',
        }),
      ),
    )
    const { router } = renderApp({
      initialEntries: ['/golden-configs/asst-1?source=bookmark#config'],
      authState: authenticatedState(),
    })

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Test preset' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Deep-linked config' }),
    ).toBeInTheDocument()
    await waitFor(() => expect(router.state.location.pathname).toBe('/test-presets/asst-1'))
    expect(router.state.location.search).toBe('?source=bookmark')
    expect(router.state.location.hash).toBe('#config')
  })

  it('falls through unknown paths to the Not Found placeholder', async () => {
    renderApp({ initialEntries: ['/no-such-screen'], authState: authenticatedState() })

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Not Found' }),
    ).toBeInTheDocument()
  })
})
