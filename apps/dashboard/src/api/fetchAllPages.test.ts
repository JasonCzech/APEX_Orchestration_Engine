import { describe, expect, it, vi } from 'vitest'

import { fetchAllOffsetPages, findInOffsetPages } from './fetchAllPages'

describe('fetchAllOffsetPages', () => {
  it('collects pages until the server returns a short page', async () => {
    const fetchPage = vi
      .fn<(limit: number, offset: number) => Promise<number[]>>()
      .mockResolvedValueOnce([1, 2])
      .mockResolvedValueOnce([3])

    await expect(
      fetchAllOffsetPages({ label: 'Rows', pageSize: 2, fetchPage }),
    ).resolves.toEqual([1, 2, 3])
    expect(fetchPage.mock.calls).toEqual([
      [2, 0],
      [2, 2],
    ])
  })

  it('fails visibly instead of looping or returning an unprovably complete capped page', async () => {
    const fetchPage = vi.fn().mockResolvedValue([1, 2])

    await expect(
      fetchAllOffsetPages({ label: 'Rows', pageSize: 2, maxOffset: 2, fetchPage }),
    ).rejects.toThrow('Rows could not be loaded completely')
    expect(fetchPage).toHaveBeenCalledTimes(2)
  })

  it('stops a bounded lookup on the first matching page', async () => {
    const fetchPage = vi
      .fn<(limit: number, offset: number) => Promise<number[]>>()
      .mockResolvedValueOnce([1, 2])
      .mockResolvedValueOnce([3, 4])

    await expect(
      findInOffsetPages({
        label: 'Rows',
        pageSize: 2,
        fetchPage,
        predicate: (item) => item === 3,
      }),
    ).resolves.toBe(3)
    expect(fetchPage.mock.calls).toEqual([
      [2, 0],
      [2, 2],
    ])
  })

  it('bounds a lookup whose endpoint never returns a short page or match', async () => {
    const fetchPage = vi.fn().mockResolvedValue([1, 2])

    await expect(
      findInOffsetPages({
        label: 'Rows',
        pageSize: 2,
        maxOffset: 2,
        fetchPage,
        predicate: () => false,
      }),
    ).rejects.toThrow('Rows could not be loaded completely')
    expect(fetchPage).toHaveBeenCalledTimes(2)
  })
})
