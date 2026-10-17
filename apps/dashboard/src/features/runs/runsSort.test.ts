import { describe, expect, it } from 'vitest'
import { RUN_BUSY } from './runsTestHandlers'
import { sortRunPage } from './runsSort'

describe('sortRunPage', () => {
  const first = { ...RUN_BUSY, thread_id: 'a', title: 'Run 2', created_at: '2026-10-16T00:00:00Z' }
  const second = { ...RUN_BUSY, thread_id: 'b', title: 'Run 10', created_at: '2026-10-17T00:00:00Z' }
  const unknown = { ...RUN_BUSY, thread_id: 'c', created_at: 'invalid' }
  it.each(['asc', 'desc'] as const)('keeps unavailable timestamps last in %s order', direction => {
    const source = [unknown, second, first]
    expect(sortRunPage(source, { key: 'created', direction }).map(run => run.thread_id)).toEqual(direction === 'asc' ? ['a', 'b', 'c'] : ['b', 'a', 'c'])
    expect(source).toEqual([unknown, second, first])
  })
  it('uses natural title sorting and keeps equal values stable', () => {
    expect(sortRunPage([second, first], { key: 'title', direction: 'asc' })).toEqual([first, second])
    expect(sortRunPage([second, first], { key: 'project', direction: 'asc' })).toEqual([second, first])
  })
  it('does not infer elapsed duration or progress when not known', () => {
    const missing = { ...first, phase_strip: [], created_at: null }
    const measured = { ...second, updated_at: '2026-10-17T00:01:00Z' }
    expect(sortRunPage([missing, measured], { key: 'elapsed', direction: 'desc' })).toEqual([measured, missing])
    expect(sortRunPage([missing, measured], { key: 'progress', direction: 'asc' })).toEqual([measured, missing])
  })
})
