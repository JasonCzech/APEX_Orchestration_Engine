import { screen, within, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { server } from '@/test/server'
import { gatedRun, gatedDetail, detailsHandler, mutableListHandler } from '@/features/approvals/__tests__/approvalsTestHandlers'
import { authenticatedState, renderApp } from '@/test/render'

vi.mock('@/components/hooks/useCompactLayout', () => ({ useCompactLayout: () => true }))

describe('compact operator navigation', () => {
  it('exposes grouped destinations, traps focus, and returns focus on Escape', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('operator') })
    const trigger = await screen.findByRole('button', { name: 'Open navigation' })
    await user.click(trigger)
    const dialog = screen.getByRole('dialog', { name: 'Navigation' })
    expect(within(dialog).getByText('Testing')).toBeInTheDocument()
    expect(within(dialog).getByText('Resources')).toBeInTheDocument()
    expect(within(dialog).getByText('Investigation')).toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: 'Logs' })).toBeInTheDocument()
    expect(within(dialog).getByRole('link', { name: 'Agent analytics' })).toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: /Account options/ })).toBeInTheDocument()
    expect(within(dialog).queryByRole('link', { name: 'Consumers' })).not.toBeInTheDocument()
    for (let index = 0; index < 30; index++) {
      await user.tab()
      expect(dialog.contains(document.activeElement)).toBe(true)
    }
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
  })
  it('isolates the drawer from approval-page keyboard shortcuts', async () => {
    const run = gatedRun('drawer-review', { kind: 'phase_review', phase: 'reporting', interruptId: 'drawer-gate', updatedAt: new Date().toISOString(), title: 'Drawer review' })
    server.use(mutableListHandler([run]).handler, detailsHandler({ [run.thread_id]: gatedDetail(run) }))
    const user = userEvent.setup()
    const { router } = renderApp({ initialEntries: ['/approvals'], authState: authenticatedState('operator') })
    await screen.findByRole('option', { name: /Drawer review/ })
    await waitFor(() => expect(router.state.location.pathname).toBe('/runs/drawer-review/reviews/drawer-gate'))
    const reviewPath = router.state.location.pathname
    await user.click(screen.getByRole('button', { name: 'Open navigation' }))
    await user.keyboard('o')
    expect(router.state.location.pathname).toBe(reviewPath)
    expect(screen.getByRole('dialog', { name: 'Navigation' })).toBeInTheDocument()
    await user.keyboard('{Enter}')
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
  })
  it('closes on navigation and focuses the content landmark', async () => {
    const user = userEvent.setup()
    renderApp({ authState: authenticatedState('operator') })
    await user.click(await screen.findByRole('button', { name: 'Open navigation' }))
    await user.click(screen.getByRole('link', { name: 'Tests' }))
    await waitFor(() => expect(screen.getByRole('main')).toHaveFocus())
    expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument()
  })
})
