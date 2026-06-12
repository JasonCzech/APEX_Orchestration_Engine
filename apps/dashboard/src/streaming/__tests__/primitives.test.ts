/**
 * Unit coverage for the streaming primitives: RingBuffer, the 50ms-floor
 * flush gate, the sessionStorage resume store, and backoff bounds.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resumeStore } from '../resumeStore'
import { RingBuffer } from '../ringBuffer'
import { createFlushGate } from '../tokenBuffer'
import { BACKOFF_CAP_MS, backoffDelayMs } from '../usePipelineStream'

describe('RingBuffer', () => {
  it('keeps only the newest `capacity` items, oldest → newest', () => {
    const ring = new RingBuffer<number>(3)
    expect(ring.toArray()).toEqual([])
    ring.push(1)
    ring.push(2)
    expect(ring.toArray()).toEqual([1, 2])
    expect(ring.size).toBe(2)
    ring.push(3)
    ring.push(4)
    ring.push(5)
    expect(ring.toArray()).toEqual([3, 4, 5])
    expect(ring.size).toBe(3)
    ring.clear()
    expect(ring.toArray()).toEqual([])
    expect(ring.size).toBe(0)
  })

  it('rejects a non-positive capacity', () => {
    expect(() => new RingBuffer(0)).toThrow()
  })
})

describe('createFlushGate', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('coalesces a burst of markDirty calls into one flush after the floor', async () => {
    const flush = vi.fn()
    const gate = createFlushGate(flush, 50)
    for (let i = 0; i < 100; i += 1) gate.markDirty()
    expect(flush).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(100) // 50ms floor + rAF tick
    expect(flush).toHaveBeenCalledTimes(1)
  })

  it('enforces the floor between consecutive flushes (≤ ~20/s)', async () => {
    const flush = vi.fn()
    const gate = createFlushGate(flush, 50)
    // Mark dirty continuously across 1s of fake time.
    for (let t = 0; t < 100; t += 1) {
      gate.markDirty()
      await vi.advanceTimersByTimeAsync(10)
    }
    await vi.advanceTimersByTimeAsync(100)
    expect(flush.mock.calls.length).toBeGreaterThan(0)
    expect(flush.mock.calls.length).toBeLessThanOrEqual(Math.ceil(1_100 / 50))
  })

  it('does not flush when clean; flushNow flushes a dirty gate immediately', () => {
    const flush = vi.fn()
    const gate = createFlushGate(flush, 50)
    gate.flushNow()
    expect(flush).not.toHaveBeenCalled()
    gate.markDirty()
    gate.flushNow()
    expect(flush).toHaveBeenCalledTimes(1)
  })

  it('cancel drops pending flushes and ignores later markDirty calls', async () => {
    const flush = vi.fn()
    const gate = createFlushGate(flush, 50)
    gate.markDirty()
    gate.cancel()
    gate.markDirty()
    await vi.advanceTimersByTimeAsync(500)
    expect(flush).not.toHaveBeenCalled()
  })
})

describe('resumeStore', () => {
  beforeEach(() => {
    window.sessionStorage.clear()
  })

  it('round-trips per (threadId, runId) and clears independently', () => {
    expect(resumeStore.get('t1', 'r1')).toBeNull()
    resumeStore.set('t1', 'r1', 'ev-1')
    resumeStore.set('t1', 'r2', 'ev-9')
    expect(resumeStore.get('t1', 'r1')).toBe('ev-1')
    expect(resumeStore.get('t1', 'r2')).toBe('ev-9')
    resumeStore.clear('t1', 'r1')
    expect(resumeStore.get('t1', 'r1')).toBeNull()
    expect(resumeStore.get('t1', 'r2')).toBe('ev-9')
  })
})

describe('backoffDelayMs', () => {
  it('grows 1s → 15s with bounded jitter and caps at 15s', () => {
    expect(backoffDelayMs(1, () => 0)).toBe(1_000)
    expect(backoffDelayMs(1, () => 1)).toBe(1_250)
    expect(backoffDelayMs(2, () => 0)).toBe(2_000)
    expect(backoffDelayMs(5, () => 0)).toBe(15_000)
    expect(backoffDelayMs(50, () => 1)).toBe(BACKOFF_CAP_MS)
  })
})
