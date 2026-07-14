/**
 * Pure reducer behind usePipelineStream: connection lifecycle + the
 * low-frequency event projections (phaseProgress, toolCalls, pendingGateHint).
 * engine_poll samples NEVER pass through here per event — they accumulate in a
 * ring buffer ref and arrive as a single coalesced 'engine_flush' action
 * (≤ ~20/s, see tokenBuffer.ts).
 */
import type {
  AgentEvent,
  EnginePollErrorEvent,
  EnginePollSample,
  GateName,
  PhaseName,
  PhaseStatus,
  PipelineEvent,
  ToolCallEvent,
} from '@apex/pipeline-events'

export type StreamStatus = 'idle' | 'connecting' | 'live' | 'reconnecting' | 'ended' | 'error'

export interface PhaseProgress {
  status: PhaseStatus
  attempt: number
}

export interface PendingGateHint {
  gate: GateName
  phase: PhaseName
}

export interface EngineStatsView {
  /** Coalesced ring-buffer snapshot, oldest → newest (cap ENGINE_SAMPLE_CAP). */
  samples: EnginePollSample[]
  latest: EnginePollSample | null
}

export interface PipelineStreamView {
  status: StreamStatus
  /** phases_plan from plan_resolved, null until it arrives. */
  plan: PhaseName[] | null
  phaseProgress: Partial<Record<PhaseName, PhaseProgress>>
  /** Live tool-call feed, capped at TOOL_CALL_CAP (oldest dropped). */
  toolCalls: ToolCallEvent[]
  /** Real-agent completion/error telemetry, capped independently. */
  agentEvents: AgentEvent[]
  /** Retryable external-engine poll failures, capped independently. */
  engineErrors: EnginePollErrorEvent[]
  engineStats: EngineStatsView
  /** Set by gate_opened; D3's gate machine consumes it. Cleared when the phase moves on. */
  pendingGateHint: PendingGateHint | null
  /** Custom events rejected by the zod contract (schema drift). */
  driftCount: number
  error?: Error
}

export const TOOL_CALL_CAP = 200
export const AGENT_EVENT_CAP = 200
export const ENGINE_ERROR_CAP = 200

/** Low-frequency pipeline events the reducer projects (engine_poll excluded by design). */
export type ReducedPipelineEvent = Exclude<PipelineEvent, { type: 'engine_poll' }>

export type StreamAction =
  | { type: 'reset' }
  | { type: 'connecting' }
  | { type: 'live' }
  | { type: 'reconnecting'; error: Error }
  | { type: 'ended' }
  | { type: 'failed'; error: Error }
  | { type: 'pipeline_event'; event: ReducedPipelineEvent }
  | { type: 'drift' }
  | { type: 'engine_flush'; samples: EnginePollSample[]; latest: EnginePollSample | null }

export const initialStreamView: PipelineStreamView = {
  status: 'idle',
  plan: null,
  phaseProgress: {},
  toolCalls: [],
  agentEvents: [],
  engineErrors: [],
  engineStats: { samples: [], latest: null },
  pendingGateHint: null,
  driftCount: 0,
}

const GATE_AWAIT_STATUS: Record<GateName, PhaseStatus> = {
  prompt_review: 'awaiting_prompt_review',
  phase_review: 'awaiting_output_review',
}

function isAwaiting(status: PhaseStatus): boolean {
  return status === 'awaiting_prompt_review' || status === 'awaiting_output_review'
}

function applyEvent(state: PipelineStreamView, event: ReducedPipelineEvent): PipelineStreamView {
  switch (event.type) {
    case 'plan_resolved': {
      const phaseProgress = { ...state.phaseProgress }
      for (const phase of event.phases) {
        phaseProgress[phase] ??= { status: 'pending', attempt: 0 }
      }
      return { ...state, plan: event.phases, phaseProgress }
    }
    case 'phase_status': {
      const hint = state.pendingGateHint
      const clearsHint = hint !== null && hint.phase === event.phase && !isAwaiting(event.status)
      return {
        ...state,
        phaseProgress: {
          ...state.phaseProgress,
          [event.phase]: { status: event.status, attempt: event.attempt },
        },
        pendingGateHint: clearsHint ? null : hint,
      }
    }
    case 'gate_opened': {
      return {
        ...state,
        pendingGateHint: { gate: event.gate, phase: event.phase },
        phaseProgress: {
          ...state.phaseProgress,
          [event.phase]: { status: GATE_AWAIT_STATUS[event.gate], attempt: event.attempt },
        },
      }
    }
    case 'tool_call': {
      const toolCalls =
        state.toolCalls.length >= TOOL_CALL_CAP
          ? [...state.toolCalls.slice(state.toolCalls.length - TOOL_CALL_CAP + 1), event]
          : [...state.toolCalls, event]
      return { ...state, toolCalls }
    }
    case 'agent_message':
    case 'agent_error': {
      const agentEvents =
        state.agentEvents.length >= AGENT_EVENT_CAP
          ? [...state.agentEvents.slice(state.agentEvents.length - AGENT_EVENT_CAP + 1), event]
          : [...state.agentEvents, event]
      return { ...state, agentEvents }
    }
    case 'engine_poll_error': {
      const engineErrors =
        state.engineErrors.length >= ENGINE_ERROR_CAP
          ? [...state.engineErrors.slice(state.engineErrors.length - ENGINE_ERROR_CAP + 1), event]
          : [...state.engineErrors, event]
      return { ...state, engineErrors }
    }
  }
}

export function streamReducer(state: PipelineStreamView, action: StreamAction): PipelineStreamView {
  switch (action.type) {
    case 'reset':
      return initialStreamView
    case 'connecting':
      // Preserves accumulated projections: a visibility rejoin tails an
      // already-rendered run; the (threadId, runId) change path resets first.
      return { ...state, status: 'connecting', error: undefined }
    case 'live':
      // Dispatched per stream part — returning the identical reference lets
      // React bail out of re-renders while already live.
      return state.status === 'live' ? state : { ...state, status: 'live', error: undefined }
    case 'reconnecting':
      return { ...state, status: 'reconnecting', error: action.error }
    case 'ended':
      return { ...state, status: 'ended' }
    case 'failed':
      return { ...state, status: 'error', error: action.error }
    case 'pipeline_event':
      return applyEvent(state, action.event)
    case 'drift':
      return { ...state, driftCount: state.driftCount + 1 }
    case 'engine_flush':
      return { ...state, engineStats: { samples: action.samples, latest: action.latest } }
  }
}
