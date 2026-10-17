import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, Link, Outlet, RouterProvider } from 'react-router'
import { describe, expect, it } from 'vitest'
import { NavigationGuards } from '@/components/NavigationGuards'
import { GateDraftGuard } from '../GateDraftGuard'

describe('Gate draft navigation', () => {
  it('blocks leaving and allows either staying or retaining the draft for later', async () => {
    const router = createMemoryRouter([{ element: <NavigationGuards><Outlet /></NavigationGuards>, children: [
      { path: '/', element: <><GateDraftGuard dirty /><Link to="/other">Another gate</Link></> },
      { path: '/other', element: <h1>Other gate</h1> },
    ] }])
    render(<RouterProvider router={router} />)
    const user = userEvent.setup()
    await user.click(screen.getByRole('link', { name: 'Another gate' }))
    expect(screen.getByRole('dialog', { name: 'Unsaved review edits' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Continue reviewing' }))
    expect(router.state.location.pathname).toBe('/')
    await user.click(screen.getByRole('link', { name: 'Another gate' }))
    await user.click(screen.getByRole('button', { name: 'Leave and keep draft' }))
    expect(await screen.findByRole('heading', { name: 'Other gate' })).toBeInTheDocument()
  })
})
