import { screen } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'
import { server } from '@/test/server'

describe('router', () => {
  it.each([
    ['/approvals', 'Approvals'],
    // /prompts became a real screen in D5; / became the real Home dashboard in
    // D7 (covered in features/home tests — its headings are content-driven);
    // /golden-configs became real in D7 too — /runs/compare is the last
    // placeholder-backed route.
    ['/runs/compare', 'Compare Runs'],
    ['/admin/system', 'System'],
  ])('renders the %s placeholder inside the shell', async (path, title) => {
    renderApp({ initialEntries: [path], authState: authenticatedState() })

    // Placeholder body (h2) + topbar title from the route handle (h1).
    expect(await screen.findByRole('heading', { level: 2, name: title })).toBeInTheDocument()
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
    renderApp({
      initialEntries: ['/golden-configs/asst-1'],
      authState: authenticatedState(),
    })

    expect(
      await screen.findByRole('heading', { level: 1, name: 'Golden Config' }),
    ).toBeInTheDocument()
    expect(
      await screen.findByRole('heading', { level: 2, name: 'Deep-linked config' }),
    ).toBeInTheDocument()
  })

  it('falls through unknown paths to the Not Found placeholder', async () => {
    renderApp({ initialEntries: ['/no-such-screen'], authState: authenticatedState() })

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Not Found' }),
    ).toBeInTheDocument()
  })
})
