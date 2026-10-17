import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { Pagination } from './Pagination'

describe('Pagination', () => {
  it('changes page size and resets the offset in a single update', async () => {
    const onChange = vi.fn()
    render(<Pagination label="Events" offset={40} limit={20} options={[10, 20, 50]} hasNext pending={false} onChange={onChange} />)
    await userEvent.selectOptions(screen.getByRole('combobox', { name: 'Events rows per page' }), '50')
    expect(onChange).toHaveBeenCalledExactlyOnceWith({ offset: 0, limit: 50 })
  })
  it('honors endpoint limits and disables navigation while loading', () => {
    render(<Pagination label="Events" offset={0} limit={20} options={[10, 20]} hasNext={false} pending onChange={vi.fn()} />)
    expect(screen.getAllByRole('option').map(option => option.textContent)).toEqual(['10', '20'])
    expect(screen.getByRole('button', { name: 'Previous' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(screen.getByRole('combobox')).toBeDisabled()
  })
})
