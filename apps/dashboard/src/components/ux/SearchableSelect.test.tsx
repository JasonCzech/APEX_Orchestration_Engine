import { useState } from 'react'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SearchableSelect } from './SearchableSelect'

const options = [
  { value: '', label: 'All tests' },
  { value: 'run-a', label: 'Checkout', description: 'Oct 17 · Failed · run-a' },
  { value: 'run-b', label: 'Checkout', description: 'Oct 16 · Completed · run-b' },
]

afterEach(() => vi.unstubAllGlobals())

describe('SearchableSelect', () => {
  it('filters duplicate titles by identity and commits with the keyboard', async () => {
    function Example() { const [value, setValue] = useState('run-a'); return <SearchableSelect label="Test" value={value} options={options} onChange={setValue} /> }
    const user = userEvent.setup()
    render(<Example />)
    const input = screen.getByRole('combobox', { name: 'Test' })
    expect(input).toHaveAccessibleDescription('Oct 17 · Failed · run-a')
    await user.click(input)
    await user.type(input, 'run-b')
    const list = screen.getByRole('listbox')
    expect(within(list).getAllByRole('option')).toHaveLength(1)
    await user.keyboard('{Enter}')
    expect(input).toHaveValue('Checkout')
    expect(input).toHaveAccessibleDescription('Oct 16 · Completed · run-b')
    expect(input).toHaveAttribute('aria-expanded', 'false')
    expect(input).toHaveFocus()
    await user.click(input)
    expect(input).toHaveAttribute('aria-expanded', 'true')
    await user.click(within(screen.getByRole('listbox')).getByRole('option', { name: /run-a/ }))
    expect(input).toHaveAccessibleDescription('Oct 17 · Failed · run-a')
  })
  it('keeps the selected option active on focus and never commits an unmatched draft', async () => {
    const changed = vi.fn()
    const user = userEvent.setup()
    render(<SearchableSelect label="Test" value="run-b" options={options} onChange={changed} />)
    const input = screen.getByRole('combobox')
    await user.click(input)
    expect(document.getElementById(input.getAttribute('aria-activedescendant')!)).toHaveAttribute('aria-selected', 'true')
    await user.type(input, 'does not exist')
    expect(screen.getByText('No matching choices.')).toBeInTheDocument()
    await user.keyboard('{Enter}{Escape}')
    expect(changed).not.toHaveBeenCalled()
    expect(input).toHaveValue('Checkout')
    expect(input).not.toHaveAttribute('aria-activedescendant')
  })
  it('keeps the active keyboard option visible in a long list', async () => {
    const scroll = vi.fn()
    const original = Element.prototype.scrollIntoView
    Element.prototype.scrollIntoView = scroll
    try {
      const user = userEvent.setup()
      render(<SearchableSelect label="Long list" value="" options={Array.from({ length: 30 }, (_, index) => ({ value: String(index), label: `Test ${index}` }))} onChange={vi.fn()} />)
      await user.click(screen.getByRole('combobox'))
      await user.keyboard('{ArrowDown>20/}')
      expect(scroll).toHaveBeenLastCalledWith({ block: 'nearest' })
      expect(scroll.mock.contexts.at(-1)).toHaveTextContent('Test 20')
    } finally { Element.prototype.scrollIntoView = original }
  })

})
