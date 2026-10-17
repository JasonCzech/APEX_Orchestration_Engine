import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { OverflowMenu } from './OverflowMenu'

describe('OverflowMenu', () => {
  it('opens from either arrow, skips disabled items, and restores focus on escape', async () => {
    const user = userEvent.setup()
    render(<OverflowMenu label="Run actions" items={[
      { label: 'Open', onSelect: vi.fn() }, { label: 'Unavailable', disabled: true, onSelect: vi.fn() },
      { label: 'Re-run', onSelect: vi.fn() },
    ]} />)
    const trigger = screen.getByRole('button', { name: 'Run actions' })
    trigger.focus()
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('menuitem', { name: 'Re-run' })).toHaveFocus()
    expect(trigger).toHaveAttribute('aria-controls', screen.getByRole('menu').id)
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('menuitem', { name: 'Open' })).toHaveFocus()
    await user.keyboard('{End}')
    expect(screen.getByRole('menuitem', { name: 'Re-run' })).toHaveFocus()
    await user.keyboard('{Home}{Escape}')
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    expect(trigger).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Open' })).toHaveFocus()
  })

  it('keeps a menu of temporarily disabled actions inspectable', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    const { rerender } = render(<OverflowMenu label="Actions" items={[{ label: 'Save', disabled: true, onSelect }, { label: 'Close', disabled: true, onSelect }]} />)
    await user.click(screen.getByRole('button', { name: 'Actions' }))
    expect(screen.getByRole('menu')).toHaveFocus()
    expect(screen.getByRole('menuitem', { name: 'Save' })).toBeDisabled()
    rerender(<OverflowMenu label="Actions" items={[{ label: 'Save', onSelect }, { label: 'Close', onSelect }]} />)
    await user.keyboard('{ArrowUp}')
    expect(screen.getByRole('menuitem', { name: 'Close' })).toHaveFocus()
    await user.keyboard('{ArrowDown}')
    expect(screen.getByRole('menuitem', { name: 'Save' })).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(onSelect).toHaveBeenCalledTimes(1)
  })

  it('closes on Tab without trapping focus and selects exactly once', async () => {
    const user = userEvent.setup()
    const onSelect = vi.fn()
    render(<><OverflowMenu label="Actions" items={[{ label: 'Open', onSelect }]} /><button>Next control</button></>)
    await user.click(screen.getByRole('button', { name: 'Actions' }))
    await user.tab()
    expect(screen.getByRole('button', { name: 'Next control' })).toHaveFocus()
    expect(screen.queryByRole('menu')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Actions' }))
    await user.keyboard('{Enter}')
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Actions' })).toHaveFocus()
  })
})
