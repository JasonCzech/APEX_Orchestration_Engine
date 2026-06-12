import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'

import { authenticatedState, renderApp } from '@/test/render'

describe('Sidebar', () => {
  it('hides the ADMIN section for viewers', async () => {
    renderApp({ authState: authenticatedState('viewer') })

    expect(await screen.findByText('Operate')).toBeInTheDocument()
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Connections' })).not.toBeInTheDocument()
  })

  it('hides the ADMIN section for operators', async () => {
    renderApp({ authState: authenticatedState('operator') })

    expect(await screen.findByText('Operate')).toBeInTheDocument()
    expect(screen.queryByText('Admin')).not.toBeInTheDocument()
  })

  it('shows the ADMIN section for admins', async () => {
    renderApp({ authState: authenticatedState('admin') })

    expect(await screen.findByText('Admin')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Connections' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Consumers' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'System' })).toBeInTheDocument()
  })

  it('toggles the collapsed class via the collapse button', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState() })

    const sidebar = await screen.findByTestId('sidebar')
    expect(sidebar).not.toHaveClass('collapsed')

    await user.click(screen.getByRole('button', { name: 'Collapse sidebar' }))
    expect(sidebar).toHaveClass('collapsed')

    await user.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(sidebar).not.toHaveClass('collapsed')
  })
})
