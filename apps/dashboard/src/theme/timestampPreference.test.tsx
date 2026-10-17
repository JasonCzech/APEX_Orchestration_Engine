import { useEffect, useState } from 'react'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { formatTimestamp } from '@/utils/time'
import { PREFERENCE_KEY, readPreferences, usePreferences, useTimestampPreference } from './preferences'

const instant = '2026-10-17T12:30:00Z'

describe('timestamp preference subscription', () => {
  it('updates mounted timestamps locally and across tabs without resetting an edit', async () => {
    let mounts = 0
    function TimestampEditor() {
      useTimestampPreference()
      const [draft, setDraft] = useState('')
      useEffect(() => { mounts += 1 }, [])
      return <><input aria-label="Unsaved instructions" value={draft} onChange={event => setDraft(event.target.value)} /><time>{formatTimestamp(instant)}</time></>
    }
    function Controls() {
      const { setPreferences } = usePreferences()
      return <button onClick={() => setPreferences({ timeZone: 'UTC' })}>Use UTC</button>
    }
    const user = userEvent.setup()
    render(<><TimestampEditor /><Controls /></>)
    const local = formatTimestamp(instant)
    await user.type(screen.getByLabelText('Unsaved instructions'), 'Keep my draft')
    await user.click(screen.getByRole('button', { name: 'Use UTC' }))
    expect(screen.getByText(formatTimestamp(instant))).toHaveTextContent('UTC')
    act(() => {
      localStorage.setItem(PREFERENCE_KEY, JSON.stringify({ timeZone: 'local' }))
      window.dispatchEvent(new StorageEvent('storage', { key: PREFERENCE_KEY }))
    })
    expect(screen.getByText(local)).toBeInTheDocument()
    expect(screen.getByLabelText('Unsaved instructions')).toHaveValue('Keep my draft')
    expect(mounts).toBe(1)
  })

  it('defaults unsupported stored time zones to local time', () => {
    localStorage.setItem(PREFERENCE_KEY, JSON.stringify({ timeZone: 'unsupported-zone' }))
    expect(readPreferences().timeZone).toBe('local')
  })
})
