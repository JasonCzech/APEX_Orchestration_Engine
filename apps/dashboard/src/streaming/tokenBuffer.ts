/**
 * rAF-coalesced flush gate with a ~50ms floor (plan Part 2 performance rule:
 * high-frequency stream data flushes to React state at most ~20fps, and ONLY
 * when dirty). usePipelineStream uses one gate per stream for engine_poll ring
 * samples; the same primitive can pace a future schema-versioned, bounded
 * custom reasoning event.
 *
 * Sequence per flush: markDirty() → setTimeout(remaining-of-floor) →
 * requestAnimationFrame → onFlush(). Both stages are fake-timer friendly.
 */
export const FLUSH_FLOOR_MS = 50

export interface FlushGate {
  /** Note new buffered data; schedules a flush unless one is already pending. */
  markDirty(): void
  /** Flush immediately if dirty (used for the trailing flush at stream end). */
  flushNow(): void
  /** Drop any pending flush and ignore future markDirty calls. */
  cancel(): void
}

export function createFlushGate(onFlush: () => void, floorMs: number = FLUSH_FLOOR_MS): FlushGate {
  let dirty = false
  let disposed = false
  let timer: ReturnType<typeof setTimeout> | null = null
  let raf: number | null = null
  let lastFlushAt = Number.NEGATIVE_INFINITY

  const flush = (): void => {
    timer = null
    raf = null
    if (disposed || !dirty) return
    dirty = false
    lastFlushAt = Date.now()
    onFlush()
  }

  return {
    markDirty(): void {
      if (disposed) return
      dirty = true
      if (timer !== null || raf !== null) return
      const wait = Math.max(0, floorMs - (Date.now() - lastFlushAt))
      timer = setTimeout(() => {
        timer = null
        if (disposed) return
        if (typeof requestAnimationFrame === 'function') {
          raf = requestAnimationFrame(() => flush())
        } else {
          flush()
        }
      }, wait)
    },
    flushNow(): void {
      if (disposed || !dirty) return
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      if (raf !== null && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(raf)
        raf = null
      }
      flush()
    },
    cancel(): void {
      disposed = true
      dirty = false
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      if (raf !== null && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(raf)
        raf = null
      }
    },
  }
}
