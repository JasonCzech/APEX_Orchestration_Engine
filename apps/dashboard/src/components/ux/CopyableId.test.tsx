import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { CopyableId } from './CopyableId'
afterEach(() => vi.restoreAllMocks())
describe('CopyableId', () => {
  it('copies the full identity and announces completion', async () => {
    const user = userEvent.setup()
    const write = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue()
    render(<CopyableId value="abc1234567890123456789" label="Version ID" />)
    await user.click(screen.getByRole('button', { name: 'Copy Version ID' }))
    expect(write).toHaveBeenCalledWith('abc1234567890123456789')
    expect(screen.getByRole('status')).toHaveTextContent('Version ID copied')
    expect(screen.getByText('abc1234567890123456789')).toBeInTheDocument()
  })
  it('exposes the full selectable identity when the clipboard rejects', async () => {
    const user = userEvent.setup()
    vi.spyOn(navigator.clipboard, 'writeText').mockRejectedValue(new Error('denied'))
    render(<CopyableId value="full-identity" />)
    await user.click(screen.getByRole('button', { name: 'Copy ID' }))
    expect(screen.getByRole('status')).toHaveTextContent('Clipboard unavailable')
    expect(screen.getByText('full-identity')).not.toHaveClass('ux-id-truncated')
  })
})
