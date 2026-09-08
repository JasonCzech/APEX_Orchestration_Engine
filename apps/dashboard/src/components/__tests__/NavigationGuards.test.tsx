import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, Link, Outlet, RouterProvider } from 'react-router'
import { expect, it } from 'vitest'
import { NavigationGuards } from '../NavigationGuards'
import { useNavigationBlocker } from '../useNavigationBlocker'

function Guard({ label, priority }: { label: string; priority: number }) {
  const blocker = useNavigationBlocker(true, priority)
  return blocker.state === 'blocked' ? (
    <div>
      <button onClick={blocker.proceed}>Continue {label}</button>
      <button onClick={blocker.reset}>Keep {label}</button>
    </div>
  ) : null
}
it('honors both key and draft safeguards without allowing one to bypass the other', async () => {
  const user = userEvent.setup()
  const router = createMemoryRouter([
    {
      element: (
        <NavigationGuards>
          <Guard label="key" priority={100} />
          <Guard label="draft" priority={0} />
          <Link to="/away">Leave</Link>
          <Outlet />
        </NavigationGuards>
      ),
      children: [
        { path: '/', element: <p>Editor</p> },
        { path: '/away', element: <p>Destination</p> },
      ],
    },
  ])
  render(<RouterProvider router={router} />)
  await user.click(screen.getByRole('link', { name: 'Leave' }))
  await user.click(await screen.findByRole('button', { name: 'Continue key' }))
  expect(screen.queryByText('Destination')).not.toBeInTheDocument()
  await user.click(await screen.findByRole('button', { name: 'Keep draft' }))
  expect(screen.getByText('Editor')).toBeInTheDocument()
  await user.click(screen.getByRole('link', { name: 'Leave' }))
  await user.click(await screen.findByRole('button', { name: 'Continue key' }))
  await user.click(
    await screen.findByRole('button', { name: 'Continue draft' }),
  )
  expect(await screen.findByText('Destination')).toBeInTheDocument()
})
