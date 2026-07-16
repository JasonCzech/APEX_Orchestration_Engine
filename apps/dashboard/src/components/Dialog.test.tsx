import { fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { Dialog } from './Dialog'

describe('Dialog', () => {
  it('focuses on open, traps tab navigation, closes on Escape, and restores focus', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    const before = document.createElement('button')
    before.type = 'button'
    before.textContent = 'Before dialog'
    document.body.append(before)
    before.focus()

    const { unmount } = render(
      <Dialog
        overlayClassName="test-overlay"
        className="test-panel"
        ariaLabel="Example dialog"
        onClose={onClose}
      >
        <button type="button">First</button>
        <button type="button">Second</button>
      </Dialog>,
    )

    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('button', { name: 'Second' })).toHaveFocus()

    await user.tab()
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus()

    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(1)

    unmount()
    expect(before).toHaveFocus()
    before.remove()
  })

  it('recaptures focus when backdrop interaction leaves focus outside a non-dismissable dialog', () => {
    const onClose = vi.fn()
    const { container, unmount } = render(
      <>
        <button type="button">Behind page</button>
        <Dialog
          overlayClassName="test-overlay"
          className="test-panel"
          ariaLabel="Pinned dialog"
          onClose={onClose}
          closeOnBackdrop={false}
        >
          <button type="button">First</button>
          <button type="button">Second</button>
        </Dialog>
      </>,
    )

    const behind = screen.getByText('Behind page')
    expect(container).toHaveAttribute('aria-hidden', 'true')
    expect(container.inert).toBe(true)

    const overlay = document.body.querySelector<HTMLElement>('.test-overlay')
    expect(overlay).not.toBeNull()
    expect(overlay?.parentElement).toHaveAttribute('id', 'apex-dialog-portal')
    expect(overlay?.parentElement).toHaveStyle({ zIndex: '1000' })
    fireEvent.mouseDown(overlay!)
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus()
    expect(onClose).not.toHaveBeenCalled()

    behind.inert = false
    behind.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(screen.getByRole('button', { name: 'First' })).toHaveFocus()

    unmount()
    expect(container.inert).not.toBe(true)
  })

  it('keeps background dialogs inert and sends Escape only to the top dialog', () => {
    const closeFirst = vi.fn()
    const closeSecond = vi.fn()
    const { unmount } = render(
      <>
        <Dialog
          overlayClassName="first-overlay"
          className="test-panel"
          ariaLabel="First dialog"
          onClose={closeFirst}
        >
          <button type="button">First action</button>
        </Dialog>
        <Dialog
          overlayClassName="second-overlay"
          className="test-panel"
          ariaLabel="Second dialog"
          onClose={closeSecond}
        >
          <button type="button">Second action</button>
        </Dialog>
      </>,
    )

    expect(document.body.querySelector('.first-overlay')).toHaveAttribute('aria-hidden', 'true')
    expect(document.body.querySelector<HTMLElement>('.first-overlay')?.inert).toBe(true)
    expect(document.body.querySelector('.second-overlay')).not.toHaveAttribute('aria-hidden')

    fireEvent.keyDown(document, { key: 'Escape' })
    expect(closeFirst).not.toHaveBeenCalled()
    expect(closeSecond).toHaveBeenCalledTimes(1)

    unmount()
  })
})
