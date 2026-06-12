import { describe, expect, it } from 'vitest'

import { formatRelative } from './time'

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
