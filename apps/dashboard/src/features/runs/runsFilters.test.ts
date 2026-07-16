import { describe, expect, it } from 'vitest'

import {
  DEFAULT_RUNS_FILTERS,
  hasActiveFilters,
  parseRunsFilters,
  RUNS_MAX_OFFSET,
  serializeRunsFilters,
  type RunsFilters,
} from './runsFilters'

describe('runsFilters', () => {
  it('round-trips a fully populated filter set through URLSearchParams', () => {
    const filters: RunsFilters = {
      status: 'interrupted',
      q: 'checkout',
      project: 'proj-alpha',
      limit: 50,
      offset: 100,
    }
    expect(parseRunsFilters(serializeRunsFilters(filters))).toEqual(filters)
  })

  it('serializes defaults to an empty query string (pristine /runs deep link)', () => {
    expect(serializeRunsFilters(DEFAULT_RUNS_FILTERS).toString()).toBe('')
    expect(parseRunsFilters(new URLSearchParams())).toEqual(DEFAULT_RUNS_FILTERS)
  })

  it('drops unknown statuses and clamps malformed numbers', () => {
    const params = new URLSearchParams(
      'status=exploded&q=%20%20&limit=999&offset=-3&project=p1',
    )
    expect(parseRunsFilters(params)).toEqual({
      project: 'p1',
      limit: 100,
      offset: 0,
    })
    // non-integer limit falls back to the default page size
    expect(parseRunsFilters(new URLSearchParams('limit=abc')).limit).toBe(25)
    expect(parseRunsFilters(new URLSearchParams('offset=999999')).offset).toBe(RUNS_MAX_OFFSET)
  })

  it('reports active filters for status/q/project but not pagination', () => {
    expect(hasActiveFilters(DEFAULT_RUNS_FILTERS)).toBe(false)
    expect(hasActiveFilters({ ...DEFAULT_RUNS_FILTERS, offset: 50 })).toBe(false)
    expect(hasActiveFilters({ ...DEFAULT_RUNS_FILTERS, q: 'soak' })).toBe(true)
    expect(hasActiveFilters({ ...DEFAULT_RUNS_FILTERS, status: 'error' })).toBe(true)
  })
})
