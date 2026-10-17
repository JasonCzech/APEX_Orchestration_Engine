import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { TabList } from './TabList'
function Example() {
  const [value, setValue] = useState('one')
  return (
    <>
      <TabList
        id="test"
        label="Evidence"
        value={value}
        onChange={setValue}
        items={[
          { id: 'one', label: 'Summary' },
          { id: 'two', label: 'Details' },
          { id: 'three', label: 'Activity' },
        ]}
      />
      <div role="tabpanel" id={`test-panel-${value}`} aria-labelledby={`test-tab-${value}`}>
        {value}
      </div>
    </>
  )
}
describe('APEX tabs', () => {
  it('supports wrapping arrows, Home, End and one focus stop with a named panel', async () => {
    const user = userEvent.setup()
    render(<Example />)
    await user.tab()
    expect(screen.getByRole('tab', { name: 'Summary' })).toHaveFocus()
    await user.keyboard('{ArrowLeft}')
    expect(screen.getByRole('tab', { name: 'Activity' })).toHaveFocus()
    expect(screen.getByRole('tabpanel', { name: 'Activity' })).toHaveTextContent('three')
    await user.keyboard('{Home}{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Details' })).toHaveFocus()
    expect(screen.getAllByRole('tab').filter((tab) => tab.tabIndex === 0)).toHaveLength(1)
    await user.keyboard('{End}{ArrowRight}')
    expect(screen.getByRole('tab', { name: 'Summary' })).toHaveFocus()
  })
  it('locks all tab interactions during a write', async () => {
    const onChange = vi.fn()
    render(<TabList disabled label="Locked tabs" value="one" onChange={onChange} items={[{ id: 'one', label: 'Summary' }, { id: 'two', label: 'Details' }]} />)
    expect(screen.getByRole('tab', { name: 'Summary' })).toBeDisabled()
    await userEvent.click(screen.getByRole('tab', { name: 'Details' }))
    expect(onChange).not.toHaveBeenCalled()
  })

})
