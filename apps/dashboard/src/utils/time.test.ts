import { describe, expect, it } from 'vitest'

import { formatRelative, formatTimestamp } from './time'
import { PREFERENCE_KEY } from '@/theme/preferences'

const NOW = Date.parse('2026-06-12T12:00:00Z')

describe('formatRelative', () => {
  it('renders an em dash for null/undefined/invalid input', () => {
    expect(formatRelative(null, NOW)).toBe('—')
    expect(formatRelative(undefined, NOW)).toBe('—')
    expect(formatRelative('not-a-date', NOW)).toBe('—')
  })

  it('formats recent timestamps relatively', () => {
    expect(formatRelative('2026-06-12T11:59:50Z', NOW)).toBe('just now')
    expect(formatRelative('2026-06-12T11:55:00Z', NOW)).toBe('5m ago')
    expect(formatRelative('2026-06-12T09:00:00Z', NOW)).toBe('3h ago')
    expect(formatRelative('2026-06-10T12:00:00Z', NOW)).toBe('2d ago')
  })

  it('falls back to a short date beyond a week', () => {
    expect(formatRelative('2026-05-01T12:00:00Z', NOW)).toMatch(/May/)
    // prior year includes the year
    expect(formatRelative('2025-05-01T12:00:00Z', NOW)).toMatch(/2025/)
  })
})


describe('formatTimestamp', () => {
  it('identifies the year and local time zone for absolute instants', () => {
    const iso = '2026-10-17T13:25:25Z'
    const timeZone = new Intl.DateTimeFormat(undefined, { timeZoneName: 'short' })
      .formatToParts(new Date(iso)).find(part => part.type === 'timeZoneName')?.value
    expect(formatTimestamp(iso)).toContain('2026')
    expect(formatTimestamp(iso)).toContain(timeZone)
  })

  it('uses UTC for absolute instants and older relative-date fallbacks when selected', () => {
    localStorage.setItem(PREFERENCE_KEY, JSON.stringify({ timeZone: 'UTC' }))
    const iso = '2026-01-01T00:30:00Z'
    expect(formatTimestamp(iso)).toContain('UTC')
    expect(formatTimestamp(iso)).toContain('2026')
    expect(formatRelative(iso, Date.parse('2026-01-10T01:00:00Z'))).toBe(
      new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' }),
    )
    expect(formatRelative('2026-06-12T11:55:00Z', NOW)).toBe('5m ago')
  })

  it('does not render an invalid or missing date as a recorded time', () => {
    for (const value of [null, undefined, '', 'bad-date']) expect(formatTimestamp(value)).toBe('—')
  })
})
