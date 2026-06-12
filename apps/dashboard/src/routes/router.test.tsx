import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'

describe('router', () => {
  it.each([
    ['/', 'Home'],
    ['/approvals', 'Approvals'],
    // /prompts became a real screen in D5; /golden-configs is still placeholder-backed.
    ['/golden-configs', 'Golden Configs'],
    ['/admin/system', 'System'],
  ])('renders the %s placeholder inside the shell', async (path, title) => {
    renderApp({ initialEntries: [path], authState: authenticatedState() })

    // Placeholder body (h2) + topbar title from the route handle (h1).
    expect(await screen.findByRole('heading', { level: 2, name: title })).toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 1, name: title })).toBeInTheDocument()
    expect(screen.getByTestId('sidebar')).toBeInTheDocument()
  })

  it('renders parameterized deep links', async () => {
    // /runs/:threadId/phases/:phase became a real screen in D1 and
    // /prompts/:ns/:name in D5; assert a route that is still
    // placeholder-backed to keep this a pure routing test.
    renderApp({
      initialEntries: ['/golden-configs/asst-1'],
      authState: authenticatedState(),
    })

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Golden Config' }),
    ).toBeInTheDocument()
  })

  it('falls through unknown paths to the Not Found placeholder', async () => {
    renderApp({ initialEntries: ['/no-such-screen'], authState: authenticatedState() })

    expect(
      await screen.findByRole('heading', { level: 2, name: 'Not Found' }),
    ).toBeInTheDocument()
  })
})
