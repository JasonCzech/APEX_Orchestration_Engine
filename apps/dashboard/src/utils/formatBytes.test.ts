import { describe, expect, it } from 'vitest'
import { formatBytes } from './formatBytes'

describe('formatBytes', () => {
  it.each([[0, '0 B'], [1023, '1023 B'], [1024, '1.0 KB'], [1048576, '1.0 MB'], [-1, '—'], [Number.NaN, '—'], [Infinity, '—']])('formats %s as %s', (bytes, expected) => {
    expect(formatBytes(bytes as number)).toBe(expected)
  })
})
