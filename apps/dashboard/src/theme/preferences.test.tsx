import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { PREFERENCE_KEY, usePreferences } from './preferences'
function PreferencesProbe() {
  const { preferences, setPreferences } = usePreferences()
  return (
    <>
      <output>{JSON.stringify(preferences)}</output>
      <button onClick={() => setPreferences({ rows: 25, wordWrap: false, reduceMotion: true })}>
        Update preferences
      </button>
    </>
  )
}
describe('personal preferences', () => {
  it('persists controls and restores them on remount', async () => {
    const user = userEvent.setup()
    const first = render(<PreferencesProbe />)
    await user.click(screen.getByRole('button'))
    expect(JSON.parse(localStorage.getItem(PREFERENCE_KEY)!)).toEqual({
      rows: 25,
      wordWrap: false,
      reduceMotion: true,
      timeZone: 'local',
    })
    expect(document.documentElement.dataset.reduceMotion).toBe('true')
    first.unmount()
    render(<PreferencesProbe />)
    expect(screen.getByRole('status')).toHaveTextContent('"rows":25')
  })
  it('recovers from malformed stored values', () => {
    localStorage.setItem(PREFERENCE_KEY, '{invalid')
    render(<PreferencesProbe />)
    expect(screen.getByRole('status')).toHaveTextContent('"rows":10')
  })
})
