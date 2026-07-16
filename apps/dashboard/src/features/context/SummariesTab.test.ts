import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  MAX_SUMMARY_STREAM_CHUNKS,
  nextSummaryStreamChunkCount,
  SUMMARY_STREAM_IDLE_MS,
  withSummaryStreamIdleDeadline,
} from './SummariesTab'

afterEach(() => vi.useRealTimers())

describe('summary stream idle deadline', () => {
  it('rejects promptly on caller abort without reporting an idle timeout', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const onTimeout = vi.fn()
    const pending = withSummaryStreamIdleDeadline(
      new Promise<never>(() => undefined),
      controller,
      onTimeout,
    )
    const rejected = expect(pending).rejects.toThrow('Unable to read the summary stream.')

    controller.abort()

    await rejected
    expect(onTimeout).not.toHaveBeenCalled()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('aborts and rejects when a transport operation never settles', async () => {
    vi.useFakeTimers()
    const controller = new AbortController()
    const onTimeout = vi.fn()
    const pending = withSummaryStreamIdleDeadline(
      new Promise<never>(() => undefined),
      controller,
      onTimeout,
    )
    const rejected = expect(pending).rejects.toThrow('Unable to read the summary stream.')

    await vi.advanceTimersByTimeAsync(SUMMARY_STREAM_IDLE_MS)

    await rejected
    expect(controller.signal.aborted).toBe(true)
    expect(onTimeout).toHaveBeenCalledOnce()
  })

  it('bounds zero-byte and tiny transport chunks independently of the byte budget', () => {
    expect(nextSummaryStreamChunkCount(MAX_SUMMARY_STREAM_CHUNKS - 1)).toBe(
      MAX_SUMMARY_STREAM_CHUNKS,
    )
    expect(() => nextSummaryStreamChunkCount(MAX_SUMMARY_STREAM_CHUNKS)).toThrow(
      'Unable to read the summary stream.',
    )
  })
})
