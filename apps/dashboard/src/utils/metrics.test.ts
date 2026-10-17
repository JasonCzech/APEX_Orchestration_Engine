import { describe, expect, it } from 'vitest'

import { formatPercentRate } from './metrics'

describe('formatPercentRate', () => {
  it('uses the same percentage precision for measured zero and nonzero rates', () => {
    expect(formatPercentRate(0)).toBe('0.00%')
    expect(formatPercentRate(0.0042)).toBe('0.42%')
    expect(formatPercentRate(1)).toBe('100.00%')
  })

  it('keeps absent and nonfinite measurements unavailable', () => {
    for (const value of [null, undefined, NaN, Infinity]) expect(formatPercentRate(value)).toBe('—')
  })
})
