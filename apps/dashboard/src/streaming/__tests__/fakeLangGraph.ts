/**
 * Scriptable fake for the LangGraph SDK client (the plan's "injectable fake
 * Client with scriptable async-generator streams"). Mirrors the verified SDK
 * surfaces consumed by src/streaming:
 *   runs.joinStream(threadId, runId, { signal?, lastEventId?, streamMode? })
 *     → AsyncGenerator<{ id?; event; data }>
 *   runs.list(threadId, { limit?, offset?, status? }) → Run[]
 *
 * Tests script each connection attempt via `scriptStream()` (consumed FIFO by
 * joinStream calls; extra calls auto-create streams so reconnect loops never
 * deadlock) and drive parts with push/end/fail. Abort signals end the
 * generator with an AbortError, like the real fetch reader.
 */
import type { Client } from '@langchain/langgraph-sdk'

export interface StreamPart {
  id?: string
  event: string
  data: unknown
}

export interface FakeRun {
  run_id: string
  status: string
  thread_id?: string
}

interface Waiter {
  resolve: (result: IteratorResult<StreamPart, undefined>) => void
  reject: (error: unknown) => void
}

function abortError(): Error {
  return new DOMException('Stream aborted', 'AbortError')
}

export class FakeRunStream {
  private queue: StreamPart[] = []
  private waiters: Waiter[] = []
  private done = false
  private failure: unknown = null
  /** True once a consumer started iterating. */
  consumed = false
  /** True once the consumer's AbortSignal fired while iterating. */
  sawAbort = false

  /** Deliver one SSE part to the consumer (or buffer it). */
  push(part: StreamPart): void {
    if (this.done || this.failure !== null) throw new Error('FakeRunStream already finished')
    const waiter = this.waiters.shift()
    if (waiter) waiter.resolve({ done: false, value: part })
    else this.queue.push(part)
  }

  /** Deliver a pipeline custom event (`event: "custom"`). */
  pushCustom(data: unknown, id?: string): void {
    this.push({ ...(id !== undefined ? { id } : {}), event: 'custom', data })
  }

  /** Server closed the stream normally (run finished). */
  end(): void {
    this.done = true
    for (const waiter of this.waiters.splice(0)) {
      waiter.resolve({ done: true, value: undefined })
    }
  }

  /** Connection dropped / server error: the generator throws. */
  fail(error: unknown): void {
    this.failure = error
    for (const waiter of this.waiters.splice(0)) waiter.reject(error)
  }

  generator(signal?: AbortSignal): AsyncGenerator<StreamPart, undefined, unknown> {
    this.consumed = true
    const next = (): Promise<IteratorResult<StreamPart, undefined>> => {
      if (signal?.aborted) {
        this.sawAbort = true
        return Promise.reject(abortError())
      }
      const part = this.queue.shift()
      if (part) return Promise.resolve({ done: false, value: part })
      if (this.failure !== null) return Promise.reject(this.failure)
      if (this.done) return Promise.resolve({ done: true, value: undefined })
      return new Promise((resolve, reject) => {
        const waiter: Waiter = {
          resolve: (result) => {
            cleanup()
            resolve(result)
          },
          reject: (error) => {
            cleanup()
            reject(error instanceof Error ? error : new Error(String(error)))
          },
        }
        const onAbort = (): void => {
          this.sawAbort = true
          const index = this.waiters.indexOf(waiter)
          if (index >= 0) this.waiters.splice(index, 1)
          reject(abortError())
        }
        const cleanup = (): void => signal?.removeEventListener('abort', onAbort)
        signal?.addEventListener('abort', onAbort, { once: true })
        this.waiters.push(waiter)
      })
    }
    return (async function* (): AsyncGenerator<StreamPart, undefined, unknown> {
      while (true) {
        const result = await next()
        if (result.done) return undefined
        yield result.value
      }
    })()
  }
}

export interface JoinStreamOptions {
  signal?: AbortSignal
  lastEventId?: string
  streamMode?: unknown
  cancelOnDisconnect?: boolean
}

export interface JoinStreamCall {
  threadId: string | null | undefined
  runId: string
  options: JoinStreamOptions | undefined
  stream: FakeRunStream
}

export interface ListCall {
  threadId: string
  options: unknown
}

export class FakeLangGraphClient {
  joinStreamCalls: JoinStreamCall[] = []
  listCalls: ListCall[] = []
  /** Result for runs.list, mutable per test. */
  listRuns: FakeRun[] = []
  private scriptedStreams: FakeRunStream[] = []

  /** Queue the stream the next joinStream call consumes. Returns it for driving. */
  scriptStream(stream: FakeRunStream = new FakeRunStream()): FakeRunStream {
    this.scriptedStreams.push(stream)
    return stream
  }

  runs = {
    joinStream: (
      threadId: string | null | undefined,
      runId: string,
      options?: JoinStreamOptions | AbortSignal,
    ): AsyncGenerator<StreamPart, undefined, unknown> => {
      const opts = options instanceof AbortSignal ? { signal: options } : options
      const stream = this.scriptedStreams.shift() ?? new FakeRunStream()
      this.joinStreamCalls.push({ threadId, runId, options: opts, stream })
      return stream.generator(opts?.signal)
    },
    list: (threadId: string, options?: unknown): Promise<FakeRun[]> => {
      this.listCalls.push({ threadId, options })
      return Promise.resolve(this.listRuns)
    },
  }

  asClient(): Client {
    return this as unknown as Client
  }
}
