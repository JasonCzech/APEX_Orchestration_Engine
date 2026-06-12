/**
 * usePipelineStream — the D2 streaming core (plan Part 2 — Data layer).
 *
 * One SSE connection per (threadId, runId) over the LangGraph SDK's
 * resumable join surface. Custom events are demuxed:
 *   plan_resolved / phase_status / gate_opened → reducer dispatch + query
 *     cache patch (applyStreamEvent);
 *   tool_call → reducer dispatch only (capped feed, low frequency);
 *   engine_poll → ring buffer ref ONLY, coalesced into state via a 50ms-floor
 *     rAF flush gate — never per-event renders, never the query cache.
 *
 * SDK surfaces (verified against node_modules/@langchain/langgraph-sdk
 * dist/client/runs/index.{d.ts,js}, dist/utils/stream.js):
 * - `client.runs.joinStream(threadId, runId, { signal?, lastEventId?,
 *   streamMode?, cancelOnDisconnect? })` → AsyncGenerator<{ id?: string;
 *   event: string; data: any }>. GET /threads/{tid}/runs/{rid}/stream with a
 *   `Last-Event-ID` header and `stream_mode` param. SSE ids ride on `part.id`.
 * - The SDK retries internally ONLY when the server advertised a reconnect
 *   path via a `location` header; all other failures surface to this hook's
 *   own backoff loop (1s..15s, jittered), which rejoins with the stored
 *   lastEventId (resumeStore, sessionStorage).
 * - Resume-window failure policy: an attempt that USED a stored id and failed
 *   clears the cursor, triggers one snapshot refetch (snapshot+tail is the
 *   correctness layer), then rejoins live.
 * - Visibility: hidden > 60s closes the stream; on visible → snapshot refetch
 *   + rejoin.
 * - Stream end/error → exactly one healing invalidate of threads.state
 *   (refetch wins — the simple monotonicity stance from the task spec).
 */
import { useEffect, useReducer } from 'react'

import { useQueryClient } from '@tanstack/react-query'

import {
  parsePipelineEvent,
  type EnginePollEvent,
  type EnginePollSample,
  type SchemaDriftReporter,
} from '@apex/pipeline-events'

import { useThreadState } from '@/api/hooks/useThreadState'
import { getLangGraphClient } from '@/api/langgraphClient'
import { queryKeys } from '@/api/queryKeys'

import { applyStreamEvent } from './applyStreamEvent'
import { resumeStore } from './resumeStore'
import { RingBuffer } from './ringBuffer'
import { initialStreamView, streamReducer, type PipelineStreamView } from './streamReducer'
import { createFlushGate } from './tokenBuffer'
import { useActiveRun } from './useActiveRun'

// Public contracts for UI consumers (exact names per the D2 plan).
export type {
  EngineStatsView,
  PendingGateHint,
  PhaseProgress,
  PipelineStreamView,
  StreamStatus,
} from './streamReducer'
export type { ToolCallEvent } from '@apex/pipeline-events'

/** engine_poll ring size (plan: 300-pt buffer behind the live-stats strip). */
export const ENGINE_SAMPLE_CAP = 300
/** Close the stream after the document has been hidden this long. */
export const HIDDEN_DISCONNECT_MS = 60_000
export const BACKOFF_BASE_MS = 1_000
export const BACKOFF_CAP_MS = 15_000

/** Exponential backoff 1s..15s with up to +25% jitter (capped at 15s). */
export function backoffDelayMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1), BACKOFF_CAP_MS)
  return Math.min(Math.round(base * (1 + random() * 0.25)), BACKOFF_CAP_MS)
}

interface StreamPart {
  id?: string
  event: string
  data: unknown
}

function abortError(): Error {
  return new DOMException('Stream aborted', 'AbortError')
}

function abortableDelay(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(abortError())
      return
    }
    const onAbort = (): void => {
      clearTimeout(timer)
      reject(abortError())
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

function toSample(event: EnginePollEvent): EnginePollSample {
  return {
    at: new Date().toISOString(),
    status: event.status,
    progress_pct: event.progress_pct,
    live_stats: event.live_stats,
  }
}

/**
 * Live view of a pipeline run. `idle` until both ids are present; the effect
 * tears down (AbortController) and resets on identity change/unmount.
 */
export function usePipelineStream(
  threadId: string | undefined,
  runId?: string | null,
): PipelineStreamView {
  const queryClient = useQueryClient()
  const [view, dispatch] = useReducer(streamReducer, initialStreamView)

  useEffect(() => {
    dispatch({ type: 'reset' })
    if (!threadId || !runId) return
    const tid = threadId
    const rid = runId

    let disposed = false
    let inner: AbortController | null = null
    let hiddenTimer: ReturnType<typeof setTimeout> | null = null
    let suspendedByVisibility = false
    let finished = false

    const ring = new RingBuffer<EnginePollSample>(ENGINE_SAMPLE_CAP)
    let latestSample: EnginePollSample | null = null
    const flushGate = createFlushGate(() => {
      dispatch({ type: 'engine_flush', samples: ring.toArray(), latest: latestSample })
    })

    const invalidateSnapshot = (): void => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.threads.state(tid) })
    }

    const reportDrift: SchemaDriftReporter = (drift) => {
      console.warn('[usePipelineStream] schema drift on custom stream event', {
        threadId: tid,
        runId: rid,
        data: drift.data,
        issues: drift.error.issues,
      })
      dispatch({ type: 'drift' })
    }

    /** Returns an Error when the part is the run's terminal `error` event. */
    const handlePart = (part: StreamPart): Error | null => {
      if (part.event === 'error') {
        const message = typeof part.data === 'string' ? part.data : JSON.stringify(part.data)
        return new Error(`Run stream reported an error: ${message}`)
      }
      // Subgraph-scoped custom events arrive as "custom|<node>:<task>"
      // (verified in the backend M1 smoke).
      if (part.event !== 'custom' && !part.event.startsWith('custom|')) return null
      const parsed = parsePipelineEvent(part.data, reportDrift)
      if (!parsed) return null
      if (parsed.type === 'engine_poll') {
        latestSample = toSample(parsed)
        ring.push(latestSample)
        flushGate.markDirty() // ring ref only — no dispatch, no cache write
        return null
      }
      dispatch({ type: 'pipeline_event', event: parsed })
      if (parsed.type !== 'tool_call') applyStreamEvent(queryClient, tid, parsed)
      return null
    }

    const streamLoop = async (signal: AbortSignal): Promise<void> => {
      let attempt = 0
      while (!signal.aborted && !disposed) {
        const resumeId = resumeStore.get(tid, rid)
        try {
          const client = await getLangGraphClient()
          const parts = client.runs.joinStream(tid, rid, {
            signal,
            streamMode: 'custom',
            ...(resumeId ? { lastEventId: resumeId } : {}),
          })
          let runError: Error | null = null
          for await (const part of parts) {
            if (signal.aborted || disposed) return
            attempt = 0
            dispatch({ type: 'live' }) // no-op (same state ref) while already live
            if (part.id) resumeStore.set(tid, rid, part.id)
            runError ??= handlePart(part)
          }
          if (signal.aborted || disposed) return
          // Natural end of the run's stream.
          finished = true
          flushGate.flushNow()
          resumeStore.clear(tid, rid)
          dispatch(runError ? { type: 'failed', error: runError } : { type: 'ended' })
          invalidateSnapshot() // exactly one healing refetch
          return
        } catch (err) {
          if (signal.aborted || disposed) return
          const error = err instanceof Error ? err : new Error(String(err))
          if (resumeId) {
            // The resume window may have expired: drop the cursor and heal
            // from the snapshot, then rejoin live.
            resumeStore.clear(tid, rid)
            invalidateSnapshot()
          }
          attempt += 1
          dispatch({ type: 'reconnecting', error })
          try {
            await abortableDelay(backoffDelayMs(attempt), signal)
          } catch {
            return
          }
        }
      }
    }

    const connect = (): void => {
      if (disposed) return
      inner?.abort()
      inner = new AbortController()
      dispatch({ type: 'connecting' })
      void streamLoop(inner.signal)
    }

    const onVisibility = (): void => {
      if (document.hidden) {
        hiddenTimer ??= setTimeout(() => {
          hiddenTimer = null
          suspendedByVisibility = true
          inner?.abort()
        }, HIDDEN_DISCONNECT_MS)
        return
      }
      if (hiddenTimer !== null) {
        clearTimeout(hiddenTimer)
        hiddenTimer = null
      }
      if (suspendedByVisibility && !disposed && !finished) {
        suspendedByVisibility = false
        invalidateSnapshot() // refetch snapshot, then rejoin live
        connect()
      }
    }

    document.addEventListener('visibilitychange', onVisibility)
    connect()

    return () => {
      disposed = true
      document.removeEventListener('visibilitychange', onVisibility)
      if (hiddenTimer !== null) clearTimeout(hiddenTimer)
      flushGate.cancel()
      inner?.abort()
    }
  }, [threadId, runId, queryClient])

  return view
}

export interface RunLiveness {
  /** Active run id for the thread, null when nothing is running/pending. */
  runId: string | null
  stream: PipelineStreamView
}

/**
 * The ONE hook the run-detail page consumes (D1 read path + D2 liveness):
 * shares the useThreadState cache entry the page already polls, discovers the
 * active run while the thread is busy, and streams it.
 */
export function useRunLiveness(threadId: string | undefined): RunLiveness {
  const thread = useThreadState(threadId)
  const runId = useActiveRun(threadId, {
    threadStatus: thread.data ? (thread.data.detail.thread_status ?? null) : undefined,
  })
  const stream = usePipelineStream(threadId, runId)
  return { runId, stream }
}
