/**
 * Structural mirror of the D2 integration contract with the streaming layer
 * (`@/streaming/usePipelineStream` — `useRunLiveness` returns
 * `{ runId, stream: PipelineStreamView }`).
 *
 * These types are deliberately LOOSER than the streaming module's own types
 * (every field optional / widened to string) so the live UI components accept
 * the real `PipelineStreamView` by structural assignment without importing the
 * streaming module's internals. Only `RunDetailPage` imports the hook itself.
 */

/** Connection lifecycle of the per-run SSE stream (LiveStatusChip states). */
export type LiveStreamStatus =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'ended'
  | 'error'

/**
 * One accumulated `tool_call` custom event. `at` is typed unknown because the
 * wire event doesn't declare it (zod .passthrough() surfaces undeclared keys
 * as unknown); render sites must guard with typeof === 'string'.
 */
export interface LiveToolCall {
  id: string
  phase: string
  tool: string
  status: string
  at?: unknown
}

/** Real-agent telemetry emitted immediately before the durable phase update. */
export type LiveAgentEvent =
  | {
      type: 'agent_message'
      phase: string
      model: string
      chars: number
      at?: unknown
    }
  | {
      type: 'agent_error'
      phase: string
      error: string
      at?: unknown
    }

/** Retryable engine status failure; the backend keeps polling up to its bounded cap. */
export interface LiveEngineError {
  type: 'engine_poll_error'
  phase: string
  attempt: number
  error: string
  consecutive_errors: number
  at?: unknown
}

/** One `engine_poll` ring-buffer sample (live_stats null when the engine has no fidelity yet). */
export interface LiveEngineSample {
  status?: string
  progress_pct?: number
  live_stats?: {
    vusers?: number | null
    tps?: number | null
    error_rate?: number | null
    p95_ms?: number | null
  } | null
  at?: string
}

/** Latest `phase_status` event per phase. */
export interface LivePhaseProgress {
  status?: string
  attempt?: number
}

/** `gate_opened` accelerator hint (string form tolerated: bare gate/phase name). */
export interface LiveGateHint {
  gate?: string | null
  phase?: string | null
  attempt?: number | null
}

/**
 * The slice of PipelineStreamView the live UI reads. The streaming agent's
 * concrete view (narrower types, required fields) assigns onto this shape.
 */
export interface LiveStreamViewLike {
  status: string
  phaseProgress?: Partial<Record<string, LivePhaseProgress>> | null
  toolCalls?: readonly LiveToolCall[] | null
  agentEvents?: readonly LiveAgentEvent[] | null
  engineErrors?: readonly LiveEngineError[] | null
  engineStats?: {
    samples?: readonly LiveEngineSample[] | null
    latest?: LiveEngineSample | null
  } | null
  pendingGateHint?: LiveGateHint | string | null
}

/** Normalizes the hint's tolerated string form to the object form. */
export function normalizeGateHint(
  hint: LiveGateHint | string | null | undefined,
): LiveGateHint | null {
  if (!hint) return null
  return typeof hint === 'string' ? { gate: hint } : hint
}
