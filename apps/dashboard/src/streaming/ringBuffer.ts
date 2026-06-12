/**
 * Fixed-capacity ring buffer for high-frequency stream data (engine_poll
 * samples, future token chunks). Lives in refs — NEVER in the react-query
 * cache or per-event component state (plan Part 2 performance rule).
 */
export class RingBuffer<T> {
  private buf: (T | undefined)[]
  /** Next write index. */
  private head = 0
  private count = 0

  constructor(readonly capacity: number) {
    if (!Number.isInteger(capacity) || capacity <= 0) {
      throw new Error(`RingBuffer capacity must be a positive integer, got ${capacity}`)
    }
    this.buf = new Array<T | undefined>(capacity)
  }

  get size(): number {
    return this.count
  }

  push(item: T): void {
    this.buf[this.head] = item
    this.head = (this.head + 1) % this.capacity
    if (this.count < this.capacity) this.count += 1
  }

  /** Snapshot oldest → newest. Allocates a fresh array (safe to hand to state). */
  toArray(): T[] {
    const out: T[] = []
    const start = this.count < this.capacity ? 0 : this.head
    for (let i = 0; i < this.count; i += 1) {
      out.push(this.buf[(start + i) % this.capacity] as T)
    }
    return out
  }

  clear(): void {
    this.buf = new Array<T | undefined>(this.capacity)
    this.head = 0
    this.count = 0
  }
}
