import { describe, expect, it } from 'vitest'
import { parseAnalyticsFilters, serializeAnalyticsFilters } from './analyticsFilters'

describe('analytics row limits', () => {
  it.each([['50', 50], ['200', 100], ['0', undefined], ['-1', undefined], ['2.5', undefined], ['bad', undefined]])('normalizes %s to %s', (raw, expected) => {
    expect(parseAnalyticsFilters(new URLSearchParams({ limit: String(raw) })).limit).toBe(expected)
  })
  it('keeps changed rows in deep links and omits the default', () => {
    expect(serializeAnalyticsFilters({ limit: 50, offset: 0 }).toString()).toBe('limit=50')
    expect(serializeAnalyticsFilters({ limit: 20 }).toString()).toBe('')
  })
})
