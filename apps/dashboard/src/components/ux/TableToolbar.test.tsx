import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { TableToolbar } from './TableToolbar'

describe('TableToolbar', () => {
  it('shows the actual filter count and clears without submitting its containing form', async () => {
    const clear = vi.fn()
    const submit = vi.fn(event => event.preventDefault())
    const { rerender } = render(<form onSubmit={submit}><TableToolbar label="Log filters" filterCount={2} onClear={clear}><input aria-label="Query" /></TableToolbar></form>)
    expect(screen.getByRole('group', { name: 'Log filters' })).toHaveTextContent('2 active filters')
    await userEvent.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(clear).toHaveBeenCalledOnce()
    expect(submit).not.toHaveBeenCalled()
    rerender(<TableToolbar label="Log filters" filterCount={0} onClear={clear}>{null}</TableToolbar>)
    expect(screen.queryByRole('button', { name: 'Clear filters' })).not.toBeInTheDocument()
  })
})
